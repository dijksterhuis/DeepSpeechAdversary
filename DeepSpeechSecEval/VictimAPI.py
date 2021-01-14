import tensorflow as tf
import numpy as np
import os
import ds_ctcdecoder

from abc import ABC
from multiprocessing import cpu_count
from cleverspeech.Utils import np_arr, lcomp

import DeepSpeech
from util.config import Config
from util.text import Alphabet


class CarliniWagnerTransforms:
    def __init__(self, audio_tensor, batch_size, sample_rate=16000, n_context=9, n_ceps=26, cep_lift=22):
        """
        Carlini & Wagners implementation of MFCC & windowing in tensorflow
        :param audio_tensor: the input audio tensor/variable
        :param audio_data: the DataLoader.AudioData test_data class
        :param batch_size: the size of the test data batch
        :param sample_rate: sample rate of the input audio files
        """
        self.filter_bank_filepath = os.path.abspath(os.path.dirname(__file__))
        self.filter_bank_filepath = os.path.join(
            self.filter_bank_filepath, "filterbanks.npy"
        )

        self.window_size = int(0.032 * sample_rate)
        self.window_step = int(0.02 * sample_rate)
        self.n_ceps = n_ceps
        self.n_contexts = n_context
        self.tot_contexts = (n_context * 2) + 1
        self.cep_lifter = cep_lift

        self.audio_input = audio_tensor
        self.batch_size = batch_size

        self.mfcc = None
        self.windowed = None
        self.features = None
        self.features_shape = None
        self.features_len = None

    def _window_generator(self, audio, size):
        for i in range(0, size - self.window_step, self.window_step):
            a = audio[:, i:i + self.window_size]
            yield a

    def _context_generator(self, feats):
        for i in range(0, feats.shape[1] - self.tot_contexts * self.n_ceps + 1, self.n_ceps):
            yield feats[:, i:i + self.tot_contexts * self.n_ceps]

    def mfcc_ops(self):
        """
        Compute the MFCC for a given audio waveform. This is
        identical to how DeepSpeech does it, but does it all in
        TensorFlow so that we can differentiate through it.
        """

        batch_size = self.batch_size
        audio = tf.cast(self.audio_input, tf.float32)

        # 1. Pre-emphasizer, a high-pass filter
        audio = tf.concat((audio[:, :1], audio[:, 1:] - 0.97 * audio[:, :-1]), 1)

        # 2. windowing into frames of 320 samples, overlapping
        size = audio.get_shape().as_list()[1]
        windowed = tf.stack(lcomp(self._window_generator(audio, size)), 1)
        window = np.hamming(self.window_size)
        windowed = windowed * window

        # 3. Take the FFT to convert to frequency space
        ffted = tf.spectral.rfft(windowed, [self.window_size])
        ffted = 1.0 / self.window_size * tf.square(tf.abs(ffted))

        # 4. Compute the Mel windowing of the FFT
        energy = tf.reduce_sum(ffted, axis=2) + np.finfo(float).eps

        filters = np.load(self.filter_bank_filepath).T
        filters = np_arr([filters for i in range(0, batch_size)], np.float32)

        feat = tf.matmul(ffted, filters) + np.finfo(float).eps

        # 5. Take the DCT again, because why not
        feat = tf.log(feat)
        feat = tf.spectral.dct(feat, type=2, norm='ortho')[:, :, :self.n_ceps]

        # 6. Amplify high frequencies for some reason
        _, n_frames, n_coeff = feat.get_shape().as_list()
        n = np.arange(n_coeff)
        lift = 1 + (self.cep_lifter/ 2.) * np.sin(np.pi * n / self.cep_lifter)
        feat = lift * feat
        width = feat.get_shape().as_list()[1]

        # 7. And now stick the energy next to the features
        self.mfcc = tf.concat(
            (tf.reshape(tf.log(energy), (-1, width, 1)), feat[:, :, 1:]),
            axis=2
        )

    def window_ops(self):
        if self.mfcc is None:
            raise AttributeError("You haven't created an mfcc value yet!")

        empty_context = np.zeros(
            (self.batch_size, self.n_contexts, self.n_ceps),
            dtype=np.float32
        )
        features = tf.concat((empty_context, self.mfcc, empty_context), 1)
        features = tf.reshape(features, [self.batch_size, -1])
        features = tf.stack(lcomp(self._context_generator(features)), 1)
        features = tf.reshape(
            features,
            [self.batch_size, -1, self.tot_contexts, self.n_ceps]
        )
        self.features = features


class Model(ABC):
    def __init__(self, sess, input_tensor, batch, beam_width=500, decoder='ds', tokens=" abcdefghijklmnopqrstuvwxyz'-"):

        self.sess = sess

        # Add DS lib to the path then run the configuration for it

        self.checkpoint_dir = os.getenv(
            "DEEPSPEECH_CHECKPOINT_DIR",
            "./deepspeech-v0.4.1-checkpoint"
        )
        self.model_dir = os.getenv(
            "DEEPSPEECH_MODEL_DIR",
            "./data"
        )
        self.tokens = tokens
        self.decoder = decoder
        self.beam_width = beam_width
        # beam_width = tf.app.flags.FLAGS.beam_width

        self.raw_logits = None
        self.logits = None
        self.inputs = None
        self.outputs = None
        self.layers = None
        self.__reset_rnn_state = None
        self.saver = None
        self.alphabet = None
        self.scorer = None

        self.initialise()
        self.create_graph(input_tensor, batch.audios.feature_lengths, batch.size)
        self.load_checkpoint()

    def __configure(self):
        DeepSpeech.create_flags()

        tf.app.flags.FLAGS.alphabet_config_path = os.path.join(
            self.model_dir,
            "alphabet.txt"
        )
        tf.app.flags.FLAGS.lm_binary_path = os.path.join(
            self.model_dir,
            "lm.binary"
        )
        tf.app.flags.FLAGS.lm_trie_path = os.path.join(
            self.model_dir,
            "trie"
        )
        tf.app.flags.FLAGS.n_steps = -1
        tf.app.flags.FLAGS.use_seq_length = True

        DeepSpeech.initialize_globals()

        self.alphabet = Alphabet(
            os.path.abspath(tf.app.flags.FLAGS.alphabet_config_path))

        self.scorer = ds_ctcdecoder.Scorer(
            tf.app.flags.FLAGS.lm_alpha,
            tf.app.flags.FLAGS.lm_beta,
            tf.app.flags.FLAGS.lm_binary_path,
            tf.app.flags.FLAGS.lm_trie_path,
            self.alphabet
        )

    def initialise(self):
        """
        Check to see if we've already initialised DeepSpeech.
        If not, create the flags and init global DS variables.
        Otherwise, delete all the flags and do the same.

        If we don't do this then the previous state is passed between different
        batches and can lead to weird issues like low decoder confidence scores
        or misspellings in transcriptions.
        """
        try:
            # does a flag value currently exist?
            assert tf.app.flags.FLAGS.train is not None
        except AttributeError:
            # no
            self.__configure()

        else:
            # yes -- see this comment:
            # https://github.com/abseil/abseil-py/issues/36#issuecomment-362367370
            for name in list(tf.app.flags.FLAGS):
                delattr(tf.app.flags.FLAGS, name)
            self.__configure()

    def create_graph(self, input_tensor, seq_length, batch_size):

        feature_extraction = CarliniWagnerTransforms(
            input_tensor,
            batch_size
        )
        feature_extraction.mfcc_ops()
        feature_extraction.window_ops()

        inputs, outputs, layers = DeepSpeech.create_inference_graph(
            input_tensor=feature_extraction.features,
            seq_length=seq_length,
            n_steps=-1,
            batch_size=batch_size,
            tflite=False
        )
        self.inputs = inputs
        self.outputs = outputs
        self.layers = layers

        self.__reset_rnn_state = outputs['initialize_state']
        self.reset_state()

        self.raw_logits = layers['raw_logits']
        self.logits = tf.transpose(outputs['outputs'], [1, 0, 2])

    def load_checkpoint(self):

        mapping = {
            v.op.name: v
            for v in tf.global_variables()
            if not v.op.name.startswith('previous_state_')
            and not v.op.name.startswith("qq")
        }

        # print("=-=-=-=- Initialised Variables")
        # for v in tf.global_variables(): print(v)

        self.saver = saver = tf.train.Saver(mapping)
        checkpoint = tf.train.get_checkpoint_state(self.checkpoint_dir)

        if not checkpoint:
            raise Exception(
                'Not a valid checkpoint directory ({})'.format(self.checkpoint_dir)
            )

        checkpoint_path = checkpoint.model_checkpoint_path
        saver.restore(self.sess, checkpoint_path)

    def get_logits(self, logits, feed):
        try:
            assert feed is not None
        except AssertionError as e:
            print("You're trying to `get_logits` without providing a feed: {e}".format(e=e))
        else:
            result = self.tf_run(logits, feed_dict=feed)
            return result

    def reset_state(self):
        self.sess.run(self.__reset_rnn_state)

    def tf_run(self, *args, **kwargs):
        self.reset_state()
        outs = self.sess.run(*args, **kwargs)
        self.reset_state()
        return outs

    def inference(self, batch, feed=None, logits=None, decoder=None, top_five=False):

        if decoder:
            decoder = decoder
        else:
            decoder = self.decoder

        if decoder == "tf":

            if top_five is True:
                raise NotImplementedError(
                    "top_five is not implemented for the tf decoder"
                )

            if logits is None:
                logits = self.get_logits(self.raw_logits, feed)

            decodings = self.tf_beam_decode(
                self.sess,
                logits,
                batch.audios.feature_lengths,
                self.tokens,
            )
            return decodings

        elif decoder == "ds" or not decoder:

            if logits is None:
                logits = self.get_logits(self.logits, feed)

            decoding_probs = [
                self.ds_decode(
                    logit,
                ) for logit in logits
            ]
            if top_five is True:
                probs = [decoding_probs[i][0] for i in range(0, 5)]
                decodings = [decoding_probs[i][1] for i in range(0, 5)]
            else:
                probs = decoding_probs[0][0][0]
                decodings = decoding_probs[0][0][1]

            return decodings, probs

        elif decoder == "batch":

            if logits is None:
                logits = self.get_logits(self.logits, feed)

            decoding_probs = self.ds_decode_batch(
                logits,
                batch.audios.feature_lengths,
            )

            if top_five is True:
                probs = [
                    decoding_probs[j][i][0] for i in range(0, 5) for j in range(batch.size)
                ]
                decodings = [
                    decoding_probs[j][i][1] for i in range(0, 5) for j in range(batch.size)
                ]
            else:
                probs = [decoding_probs[j][0][0] for j in range(batch.size)]
                decodings = [decoding_probs[j][0][1] for j in range(batch.size)]

            return decodings, probs

        elif decoder == "greedy":
            if logits is None:
                logits = self.get_logits(self.raw_logits, feed)

            decodings = self.tf_greedy_decode(
                self.sess,
                logits,
                batch.audios.feature_lengths,
                self.tokens,
            )
            return decodings

        else:
            raise Exception(
                "Please choose a valid decoder -- tf, greedy, ds or batch"
            )

    def ds_decode(self, logits):

        decoded_probs = ds_ctcdecoder.ctc_beam_search_decoder(
            np.squeeze(logits),
            Config.alphabet,
            self.beam_width,
            scorer=self.scorer
        )

        return decoded_probs

    def ds_decode_batch(self, logits, lengths):

        l = lengths[0]

        # I have 6 cores on my development machine -- I also want to do other
        # things like write papers when running experiments.

        decoded_probs = ds_ctcdecoder.ctc_beam_search_decoder_batch(
            logits,
            np.asarray([l for _ in range(logits.shape[0])], dtype=np.int32),
            Config.alphabet,
            self.beam_width,
            scorer=self.scorer,
            num_processes=cpu_count() // 2
        )

        return decoded_probs

    def tf_beam_decode(self, sess, logits, features_lengths, tokens):

        tf_decode, log_probs = tf.nn.ctc_beam_search_decoder(
            logits,
            features_lengths,
            merge_repeated=False,
            beam_width=self.beam_width
        )
        dense = tf.sparse.to_dense(tf_decode[0])
        tf_dense = self.tf_run([dense])
        tf_outputs = [''.join([
                tokens[int(x)] for x in tf_dense[0][i]
            ]) for i in range(tf_dense[0].shape[0])]

        return tf_outputs

    def tf_greedy_decode(self, sess, logits, features_lengths, tokens, merge_repeated=True):

        tf_decode, log_probs = tf.nn.ctc_greedy_decoder(
            logits,
            features_lengths,
            merge_repeated=merge_repeated,
        )
        dense = tf.sparse.to_dense(tf_decode[0])
        tf_dense = self.tf_run([dense])
        tf_outputs = [''.join([
                tokens[int(x)] for x in tf_dense[0][i]
            ]) for i in range(tf_dense[0].shape[0])]

        probs = self.tf_run(log_probs)
        probs = [prob[0] for prob in probs]
        return tf_outputs, probs
