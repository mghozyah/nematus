import logging
import sys
import time

import numpy
import tensorflow as tf

import exception
import rnn_inference
import util


"""Represents a collection of models that can be used jointly for inference.

Currently only RNN-based models are supported. Beam search can use multiple
models (i.e. an ensemble) but sampling is limited to a single model. Multi-GPU
inference is not yet supported.

TODO Multi-GPU inference (i.e. multiple replicas of the same model).
TODO Transformer support.
TODO Mixed RNN/Tranformer inference.
TODO Ensemble sampling (is this useful?).
"""
class InferenceModelSet(object):
    def __init__(self, models, configs):
        self._models = models
        self._cached_sample_graph = None
        self._cached_beam_search_graph = None

    def sample(self, session, x, x_mask, return_alignments=False):
        # Sampling is not implemented for ensembles, so just use the first
        # model.
        model = self._models[0]
        if self._cached_sample_graph is None:
            self._cached_sample_graph = rnn_inference.SampleGraph(model)
        return rnn_inference.sample(session, model, x, x_mask,
                                    self._cached_sample_graph, return_alignments=return_alignments)

    def beam_search(self, session, x, x_mask, beam_size,
                    normalization_alpha=0.0, return_alignments=False):
        """Beam search using all models contained in this model set.

        If using an ensemble (i.e. more than one model), then at each timestep
        the top k tokens are selected according to the sum of the models' log
        probabilities (where k is the beam size).

        Args:
            session: TensorFlow session.
            x: Numpy array with shape (factors, max_seq_len, batch_size).
            x_mask: Numpy array with shape (max_seq_len, batch_size).
            beam_size: beam width.
            normalization_alpha: length normalization hyperparamter.

        Returns:
            A list of lists of (translation, score) pairs. The outer list
            contains one list for each input sentence in the batch. The inner
            lists contain k elements (where k is the beam size), sorted by
            score in ascending order (i.e. best first, assuming lower scores
            are better).
        """
        if (self._cached_beam_search_graph is None
            or self._cached_beam_search_graph.beam_size != beam_size):
            self._cached_beam_search_graph = \
                rnn_inference.BeamSearchGraph(self._models, beam_size)
        return rnn_inference.beam_search(session, self._models, x, x_mask,
                                         beam_size, normalization_alpha,
                                         self._cached_beam_search_graph, return_alignments=return_alignments)


def translate_file(input_file, output_file, session, models, configs,
                   beam_size=12, nbest=False, minibatch_size=80,
                   maxibatch_size=20, normalization_alpha=1.0, return_alignments=False):
    """Translates a source file using a translation model (or ensemble).

    Args:
        input_file: file object from which source sentences will be read.
        output_file: file object to which translations will be written.
        session: TensorFlow session.
        models: list of model objects to use for beam search.
        configs: model configs.
        beam_size: beam width.
        nbest: if True, produce n-best output with scores; otherwise 1-best.
        minibatch_size: minibatch size in sentences.
        maxibatch_size: number of minibatches to read and sort, pre-translation.
        normalization_alpha: alpha parameter for length normalization.
    """

    def print_alignments(alignments, models):
        for i in range(len(alignments)):
            for j in range(models):
                print("values of word i {} in model j {} ".format(i,j))
                print(alignments[i][j])
        #print("alignments: {}".format(alignments))

    def translate_maxibatch(maxibatch, model_set, num_to_target,
                            num_prev_translated):
        """Translates an individual maxibatch.

        Args:
            maxibatch: a list of sentences.
            model_set: an InferenceModelSet object.
            num_to_target: dictionary mapping target vocabulary IDs to strings.
            num_prev_translated: the number of previously translated sentences.
        """

        # Sort the maxibatch by length and split into minibatches.
        try:
            minibatches, idxs = util.read_all_lines(configs[0], maxibatch,
                                                    minibatch_size)
        except exception.Error as x:
            logging.error(x.msg)
            sys.exit(1)

        # Translate the minibatches and store the resulting beam (i.e.
        # translations and scores) for each sentence.
        beams = []
        alignments = []
        for x in minibatches:
            y_dummy = numpy.zeros(shape=(len(x),1))
            x, x_mask, _, _ = util.prepare_data(x, y_dummy, configs[0].factors,
                                                maxlen=None)
            sample, scores = model_set.beam_search(
                session=session,
                x=x,
                x_mask=x_mask,
                beam_size=beam_size,
                normalization_alpha=normalization_alpha, 
                return_alignments=return_alignments)
            print("scores {}".format(scores.shape))
            beams.extend(sample)
            alignments.extend(scores)
            num_translated = num_prev_translated + len(beams)
            logging.info('Translated {} sents'.format(num_translated))

        if(return_alignments):
            print_alignments(alignments , len(model_set._models))

        # Put beams into the same order as the input maxibatch.
        tmp = numpy.array(beams, dtype=numpy.object)
        ordered_beams = tmp[idxs.argsort()]

        # Write the translations to the output file.
        for i, beam in enumerate(ordered_beams):
            if nbest:
                num = num_prev_translated + i
                for sent, cost in beam:
                    translation = util.seq2words(sent, num_to_target)
                    line = "{} ||| {} ||| {}\n".format(num, translation,
                                                       str(cost))
                    output_file.write(line)
            else:
                best_hypo, cost = beam[0]
                line = util.seq2words(best_hypo, num_to_target) + '\n'
                output_file.write(line)

    _, _, _, num_to_target = util.load_dictionaries(configs[0])
    model_set = InferenceModelSet(models, configs)

    logging.info("NOTE: Length of translations is capped to {}".format(
        configs[0].translation_maxlen))

    start_time = time.time()

    num_translated = 0
    maxibatch = []
    while True:
        line = input_file.readline()
        if line == "":
            if len(maxibatch) > 0:
                translate_maxibatch(maxibatch, model_set, num_to_target,
                                    num_translated)
                num_translated += len(maxibatch)
            break
        maxibatch.append(line)
        if len(maxibatch) == (maxibatch_size * minibatch_size):
            translate_maxibatch(maxibatch, model_set, num_to_target,
                                num_translated)
            num_translated += len(maxibatch)
            maxibatch = []

    duration = time.time() - start_time
    logging.info('Translated {} sents in {} sec. Speed {} sents/sec'.format(
        num_translated, duration, num_translated/duration))
