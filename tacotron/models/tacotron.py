import tensorflow as tf
from infolog import log
from tacotron.models.Architecture_wrappers import TacotronDecoderCell, TacotronEncoderCell
from tacotron.models.attention import LocationSensitiveAttention
from tacotron.models.custom_decoder import CustomDecoder
from tacotron.models.helpers import TacoTestHelper, TacoTrainingHelper
from tacotron.models.modules import *
from tacotron.utils.symbols import symbols
from tensorflow.contrib.seq2seq import dynamic_decode


class Tacotron():
	"""Tacotron-2 Feature prediction Model.
	"""
	def __init__(self, hparams):
		self._hparams = hparams


	def initialize(self, inputs, input_lengths, lf0_targets=None, mgc_targets=None, bap_targets=None, stop_token_targets=None, targets_lengths=None, gta=False,
			global_step=None, is_training=False, is_evaluating=False):
		"""
		Initializes the model for inference

		sets "lf0_outputs" and "alignments" fields.

		Args:
			- inputs: int32 Tensor with shape [N, T_in] where N is batch size, T_in is number of
			  steps in the input time series, and values are character IDs
			- input_lengths: int32 Tensor with shape [N] where N is batch size and values are the lengths
			of each sequence in inputs.
			- lf0_targets: float32 Tensor with shape [N, T_out, M] where N is batch size, T_out is number
			of steps in the output time series, M is num_lf0, and values are entries in the lf0
			spectrogram. Only needed for training.
		"""
		if lf0_targets is None and stop_token_targets is not None:
			raise ValueError('no lf0 targets were provided but token_targets were given')
		if lf0_targets is not None and stop_token_targets is None and not gta:
			raise ValueError('Mel targets are provided without corresponding token_targets')
		if gta and mgc_targets is not None:
			raise ValueError('Linear spectrogram prediction is not supported in GTA mode!')
		if is_training and self._hparams.mask_decoder and targets_lengths is None:
			raise RuntimeError('Model set to mask paddings but no targets lengths provided for the mask!')
		if is_training and is_evaluating:
			raise RuntimeError('Model can not be in training and evaluation modes at the same time!')

		with tf.variable_scope('inference') as scope:
			batch_size = tf.shape(inputs)[0]
			hp = self._hparams
			target_depth = hp.num_lf0 + hp.num_mgc + hp.num_bap
			assert hp.tacotron_teacher_forcing_mode in ('constant', 'scheduled')
			if hp.tacotron_teacher_forcing_mode == 'scheduled' and is_training:
				assert global_step is not None

			# Embeddings ==> [batch_size, sequence_length, embedding_dim]
			embedding_table = tf.get_variable(
				'inputs_embedding', [len(symbols), hp.embedding_dim], dtype=tf.float32)
			embedded_inputs = tf.nn.embedding_lookup(embedding_table, inputs)


			#Encoder Cell ==> [batch_size, encoder_steps, encoder_lstm_units]
			encoder_cell = TacotronEncoderCell(
				EncoderConvolutions(is_training, hparams=hp, scope='encoder_convolutions'),
				EncoderRNN(is_training, size=hp.encoder_lstm_units,
					zoneout=hp.tacotron_zoneout_rate, scope='encoder_LSTM'))

			encoder_outputs = encoder_cell(embedded_inputs, input_lengths)

			#For shape visualization purpose
			enc_conv_output_shape = encoder_cell.conv_output_shape


			#Decoder Parts
			#Attention Decoder Prenet
			prenet = Prenet(is_training, layers_sizes=hp.prenet_layers, drop_rate=hp.tacotron_dropout_rate, scope='decoder_prenet')
			#Attention Mechanism
			attention_mechanism = LocationSensitiveAttention(hp.attention_dim, encoder_outputs, hparams=hp,
				is_training=is_training, mask_encoder=hp.mask_encoder, memory_sequence_length=input_lengths,
				smoothing=hp.smoothing, cumulate_weights=hp.cumulative_weights)
			#Decoder LSTM Cells
			decoder_lstm = DecoderRNN(is_training, layers=hp.decoder_layers,
				size=hp.decoder_lstm_units, zoneout=hp.tacotron_zoneout_rate, scope='decoder_lstm')
			#Frames Projection layer
			frame_projection = FrameProjection(target_depth * hp.outputs_per_step, scope='mgc_transform')
			#<stop_token> projection layer
			stop_projection = StopProjection(is_training or is_evaluating, shape=hp.outputs_per_step, scope='stop_token_projection')


			#Decoder Cell ==> [batch_size, decoder_steps, target_depth * r] (after decoding)
			decoder_cell = TacotronDecoderCell(prenet, attention_mechanism, decoder_lstm, frame_projection, stop_projection)


			#Define the helper for our decoder
			if is_training or is_evaluating or gta:
				decoder_targets = tf.concat([tf.expand_dims(lf0_targets, axis=-1), mgc_targets, bap_targets], axis=-1)
				self.helper = TacoTrainingHelper(batch_size, decoder_targets, target_depth, hp, gta, is_evaluating, global_step)
			else:
				self.helper = TacoTestHelper(batch_size, target_depth, hp)


			#initial decoder state
			decoder_init_state = decoder_cell.zero_state(batch_size=batch_size, dtype=tf.float32)

			#Only use max iterations at synthesis time
			max_iters = hp.max_iters if not (is_training or is_evaluating) else None

			#Decode
			(frames_prediction, stop_token_prediction, _), final_decoder_state, _ = dynamic_decode(
				CustomDecoder(decoder_cell, self.helper, decoder_init_state),
				impute_finished=False,
				maximum_iterations=max_iters,
				swap_memory=hp.tacotron_swap_with_cpu)


			# Reshape outputs to be one output per entry
			#==> [batch_size, non_reduced_decoder_steps (decoder_steps * r), target_depth]
			decoder_outputs = tf.reshape(frames_prediction, [batch_size, -1, target_depth])
			stop_token_outputs = tf.reshape(stop_token_prediction, [batch_size, -1])


			#Postnet
			postnet = Postnet(is_training, hparams=hp, scope='postnet_convolutions')

			#Compute residual using post-net ==> [batch_size, decoder_steps * r, postnet_channels]
			residual = postnet(decoder_outputs)

			#Project residual to same dimension as target depth
			#==> [batch_size, decoder_steps * r, target_depth]
			residual_projection = FrameProjection(target_depth, scope='postnet_projection')
			projected_residual = residual_projection(residual)


			#Compute the final outputs
			final_outputs = decoder_outputs + projected_residual

			#Compute each feature outputs
			lf0_outputs = tf.slice(final_outputs, [0, 0, 0], [-1, -1, hp.num_lf0])
			lf0_outputs = tf.squeeze(lf0_outputs, axis=-1, name='lf0_outputs')
			mgc_outputs = tf.slice(final_outputs, [0, 0, hp.num_lf0], [-1, -1, hp.num_mgc], name='mgc_outputs')
			bap_outputs = tf.slice(final_outputs, [0, 0, hp.num_lf0 + hp.num_mgc], [-1, -1, hp.num_bap], name='bap_outputs')


			#Grab alignments from the final decoder state
			alignments = tf.transpose(final_decoder_state.alignment_history.stack(), [1, 2, 0], name='alignments')

			if is_training:
				self.ratio = self.helper._ratio
			self.inputs = inputs
			self.input_lengths = input_lengths
			self.decoder_outputs = decoder_outputs
			self.final_outputs = final_outputs
			self.alignments = alignments
			self.stop_token_outputs = stop_token_outputs
			self.stop_token_targets = stop_token_targets
			self.lf0_outputs = lf0_outputs
			self.mgc_outputs = mgc_outputs
			self.bap_outputs = bap_outputs
			self.lf0_targets = lf0_targets
			self.mgc_targets = mgc_targets
			self.bap_targets = bap_targets
			self.targets_lengths = targets_lengths
			log('Initialized Tacotron model. Dimensions (? = dynamic shape): ')
			log('  Train mode:               {}'.format(is_training))
			log('  Eval mode:                {}'.format(is_evaluating))
			log('  GTA mode:                 {}'.format(gta))
			log('  Synthesis mode:           {}'.format(not (is_training or is_evaluating)))
			log('  embedding:                {}'.format(embedded_inputs.shape))
			log('  enc conv out:             {}'.format(enc_conv_output_shape))
			log('  encoder out:              {}'.format(encoder_outputs.shape))
			log('  decoder out:              {}'.format(decoder_outputs.shape))
			log('  residual out:             {}'.format(residual.shape))
			log('  projected residual out:   {}'.format(projected_residual.shape))
			log('  final out:                {}'.format(final_outputs.shape))
			log('  lf0 out:                  {}'.format(tf.expand_dims(lf0_outputs, axis=-1).shape))
			log('  mgc out:                  {}'.format(mgc_outputs.shape))
			log('  bap out:                  {}'.format(bap_outputs.shape))
			log('  <stop_token> out:         {}'.format(stop_token_outputs.shape))


	def add_loss(self):
		'''Adds loss to the model. Sets "loss" field. initialize must have been called.'''
		with tf.variable_scope('loss') as scope:
			hp = self._hparams
			self.world_targets = tf.concat([tf.expand_dims(self.lf0_targets, axis=-1), self.mgc_targets, self.bap_targets], axis=-1)

			if hp.mask_decoder:
				# Compute loss of predictions before postnet
				before_loss = MaskedMSE(self.world_targets, self.decoder_outputs, self.targets_lengths, hparams=self._hparams)
				# Compute loss after postnet
				after_loss = MaskedMSE(self.world_targets, self.final_outputs, self.targets_lengths, hparams=self._hparams)
				#Compute <stop_token> loss (for learning dynamic generation stop)
				stop_token_loss = MaskedSigmoidCrossEntropy(self.stop_token_targets,
					self.stop_token_outputs, self.targets_lengths, hparams=self._hparams)

			else:
				# guided_attention loss
				N = self._hparams.max_text_length
				T = self._hparams.max_frame_num
				A = tf.pad(self.alignments, [(0, 0), (0, N), (0, T)], mode="CONSTANT", constant_values=-1.)[:, :N, :T]
				gts = tf.convert_to_tensor(GuidedAttention(N, T))
				attention_masks = tf.to_float(tf.not_equal(A, -1))
				attention_loss = tf.reduce_sum(tf.abs(A * gts) * attention_masks)
				attention_loss /= tf.reduce_sum(attention_masks)
				# Compute loss of predictions before postnet
				before_loss = tf.reduce_mean(tf.abs(self.world_targets - self.decoder_outputs))
				# Compute loss after postnet
				after_loss = tf.reduce_mean(tf.abs(self.world_targets - self.final_outputs))
				#Compute <stop_token> loss (for learning dynamic generation stop)
				stop_token_loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(
								labels=self.stop_token_targets,
								logits=self.stop_token_outputs))

			# Get all trainable variables
			all_vars = tf.trainable_variables()
			regularization_loss = tf.add_n([tf.nn.l2_loss(v) for v in all_vars
				if not('bias' in v.name or 'Bias' in v.name)]) * hp.tacotron_reg_weight

			# Compute final loss term
			self.before_loss = before_loss
			self.after_loss = after_loss
			self.stop_token_loss = stop_token_loss
			self.regularization_loss = regularization_loss
			self.attention_loss = attention_loss

			self.loss = self.before_loss + self.after_loss + self.stop_token_loss + self.regularization_loss + self.attention_loss

	def add_optimizer(self, global_step):
		'''Adds optimizer. Sets "gradients" and "optimize" fields. add_loss must have been called.

		Args:
			global_step: int32 scalar Tensor representing current global step in training
		'''
		with tf.variable_scope('optimizer') as scope:
			hp = self._hparams
			if hp.tacotron_decay_learning_rate:
				self.decay_steps = hp.tacotron_decay_steps
				self.decay_rate = hp.tacotron_decay_rate
				self.learning_rate = self._learning_rate_decay(hp.tacotron_initial_learning_rate, global_step)
			else:
				self.learning_rate = tf.convert_to_tensor(hp.tacotron_initial_learning_rate)

			optimizer = tf.train.AdamOptimizer(self.learning_rate, hp.tacotron_adam_beta1,
				hp.tacotron_adam_beta2, hp.tacotron_adam_epsilon)
			gradients, variables = zip(*optimizer.compute_gradients(self.loss))
			self.gradients = gradients
			#Just for causion
			#https://github.com/Rayhane-mamah/Tacotron-2/issues/11
			if hp.tacotron_clip_gradients:
				clipped_gradients, _ = tf.clip_by_global_norm(gradients, 1.)
			else:
				clipped_gradients = gradients

			# Add dependency on UPDATE_OPS; otherwise batchnorm won't work correctly. See:
			# https://github.com/tensorflow/tensorflow/issues/1122
			with tf.control_dependencies(tf.get_collection(tf.GraphKeys.UPDATE_OPS)):
				self.optimize = optimizer.apply_gradients(zip(clipped_gradients, variables),
					global_step=global_step)

	def _learning_rate_decay(self, init_lr, global_step):
		#################################################################
		# Narrow Exponential Decay:

		# Phase 1: lr = 1e-3
		# We only start learning rate decay after 50k steps

		# Phase 2: lr in ]1e-5, 1e-3[
		# decay reach minimal value at step 310k

		# Phase 3: lr = 1e-5
		# clip by minimal learning rate value (step > 310k)
		#################################################################
		hp = self._hparams

		#Compute natural exponential decay
		lr = tf.train.exponential_decay(init_lr,
			global_step - hp.tacotron_start_decay, #lr = 1e-3 at step 50k
			self.decay_steps,
			self.decay_rate, #lr = 1e-5 around step 310k
			name='lr_exponential_decay')


		#clip learning rate by max and min values (initial and final values)
		return tf.minimum(tf.maximum(lr, hp.tacotron_final_learning_rate), init_lr)
