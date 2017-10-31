# coding=utf-8
"""
This module contains the base class for all the machine-learned classifiers in Workbench.
"""
from __future__ import absolute_import, unicode_literals
from builtins import object

from abc import ABCMeta, abstractmethod
import copy
import hashlib
import json
import logging
import os

from sklearn.externals import joblib

from .. import markup
from ..exceptions import ClassifierLoadError
from ..core import Query

from ..models import create_model, ModelConfig

logger = logging.getLogger(__name__)


class ClassifierConfig(object):
    """A value object representing a classifier configuration

    Attributes:
        model_type (str): The name of the model type. Will be used to find the
            model class to instantiate
        model_settings (dict): Settings specific to the model type specified
        params (dict): Params to pass to the underlying classifier
        param_selection (dict): Configuration for param selection (using cross
            validation)
            {'type': 'shuffle',
            'n': 3,
            'k': 10,
            'n_jobs': 2,
            'scoring': '',
            'grid': {}
            }
        features (dict): The keys are the names of feature extractors and the
            values are either a kwargs dict which will be passed into the
            feature extractor function, or a callable which will be used as to
            extract features.
    """

    __slots__ = ['model_type', 'features', 'model_settings', 'params', 'param_selection']

    def __init__(self, model_type=None, features=None, model_settings=None, params=None,
                 param_selection=None):
        """Initializes a classifier configuration"""
        for arg, val in {'model_type': model_type, 'features': features}.items():
            if val is None:
                raise TypeError('__init__() missing required argument {!r}'.format(arg))
        if params is None and (param_selection is None or param_selection.get('grid') is None):
            raise ValueError("__init__() One of 'params' and 'param_selection' is required")
        self.model_type = model_type
        self.features = features
        self.model_settings = model_settings
        self.params = params
        self.param_selection = param_selection

    def to_dict(self):
        """Converts the model config object into a dict

        Returns:
            dict: A dict version of the config
        """
        result = {}
        for attr in self.__slots__:
            result[attr] = getattr(self, attr)
        return result

    def __repr__(self):
        args_str = ', '.join("{}={!r}".format(key, getattr(self, key)) for key in self.__slots__)
        return "{}({})".format(self.__class__.__name__, args_str)

    @classmethod
    def from_model_config(cls, model_config):
        config = model_config.to_dict()
        config.pop('example_type')
        config.pop('label_type')
        return cls(**config)

    def to_json(self):
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def _make_hash(cls, obj):
        """Makes a hash from a dictionary, list, tuple or set to any level,
        that contains only other hashable types (including any lists, tuples,
        sets, and dictionaries).
        """

        if isinstance(obj, (set, tuple, list)):
            return tuple([cls._make_hash(e) for e in obj])
        elif not isinstance(obj, dict):
            return hash(obj)

        new_o = copy.deepcopy(obj)
        for key, value in new_o.items():
            new_o[key] = cls._make_hash(value)

        return hash(tuple(frozenset(sorted(new_o.items()))))


class Classifier(object):
    """The base class for all the machine-learned classifiers in Workbench. A classifier is a
    machine-learned model that categorizes input examples into one of the pre-determined class
    labels. Among other functionality, each classifier provides means by which to fit a statistical
    model on a given training dataset and then use the trained model to make predictions on new
    unseen data."""

    __metaclass__ = ABCMeta

    CLF_TYPE = None

    def __init__(self, resource_loader):
        """Initializes a classifier

        Args:
            resource_loader (ResourceLoader): An object which can load resources for the classifier
        """
        self._resource_loader = resource_loader
        self._model = None  # will be set when model is fit or loaded
        self.ready = False
        self.dirty = False
        self.config = None
        self.hash = ''

    def fit(self, queries=None, label_set='train', previous_model_path=None, **kwargs):
        """Trains a statistical model for classification using the provided training examples and
        model configuration.

        Args:
            queries (list of ProcessedQuery): The labeled queries to use as training data
            label_set (list, optional): A label set to load. If not specified, the default
                 training set will be loaded.
            previous_model_path (str, optional): The path of a previous version of the model for
                this classifier. If the previous model is equivalent to the new one, it will be
                loaded instead. Equivalence here is determined by the model's training data and
                configuration.
            model_type (str, optional): The type of machine learning model to use. If omitted, the
                 default model type will be used.
            model_settings (dict): Settings specific to the model type specified
            features (dict): Features to extract from each example instance to form the feature
                 vector used for model training. If omitted, the default feature set for the model
                 type will be used.
            params (dict): Params to pass to the underlying classifier
            params_selection (dict): The grid of hyper-parameters to search, for finding the optimal
                 hyper-parameter settings for the model. If omitted, the default hyper-parameter
                 search grid will be used.
            param_selection (dict): Configuration for param selection (using cross-validation)
                {'type': 'shuffle',
                'n': 3,
                'k': 10,
                'n_jobs': 2,
                'scoring': '',
                'grid': { 'C': [100, 10000, 1000000]}}
            features (dict): The keys are the names of feature extractors and the
                values are either a kwargs dict which will be passed into the
                feature extractor function, or a callable which will be used as to
                extract features.

        Examples:
            Fit using default the configuration.

                >>> clf.fit()

            Fit using a 'special' label set.

                >>> clf.fit(label_set='special')

            Fit using given params, bypassing cross-validation. This is useful for speeding up
            train times if you are confident the params are optimized.

                >>> clf.fit(params={'C': 10000000})

            Fit using given parameter selection settings (also known as cross-validation settings).

                >>> clf.fit(param_selection={})

            Fit using a custom set of features, including a custom feature extractor.
            This is only for advanced users.

                >>> clf.fit(features={
                        'in-gaz': {}, // gazetteer features
                        'contrived': lambda exa, res: {'contrived': len(exa.text) == 26}
                    })
        """

        # create model with given params
        model_config = self._get_model_config(**kwargs)
        model = create_model(model_config)
        new_hash = self._get_model_hash(model_config, queries, label_set)

        if previous_model_path:
            old_hash = self._load_hash(previous_model_path)
            if old_hash == new_hash:
                logger.info('No need to fit. Loading previous model.')
                self.load(previous_model_path)
                return

        queries, classes = self._get_queries_and_labels(queries, label_set)
        if len(set(classes)) <= 1:
            logger.warning('Not doing anything for fit since there is only one class')
            return

        model.initialize_resources(self._resource_loader, queries, classes)
        model.fit(queries, classes)
        self._model = model
        self.config = ClassifierConfig.from_model_config(self._model.config)
        self.hash = new_hash

        self.ready = True
        self.dirty = True

    def predict(self, query):
        """Predicts a class label for the given query using the trained classification model

        Args:
            query (Query or str): The input query

        Returns:
            str: The predicted class label
        """
        if not self._model:
            logger.error('You must fit or load the model before running predict')
            return
        if not isinstance(query, Query):
            query = self._resource_loader.query_factory.create_query(query)

        return self._model.predict([query])[0]

    def predict_proba(self, query):
        """Runs prediction on a given query and generates multiple hypotheses with their
        associated probabilities using the trained classification model

        Args:
            query (Query): The input query

        Returns:
            list: a list of tuples of the form (str, float) grouping predicted class labels and
                their probabilities
        """
        if not self._model:
            logger.error('You must fit or load the model before running predict_proba')
            return
        if not isinstance(query, Query):
            query = self._resource_loader.query_factory.create_query(query)

        predict_proba_result = self._model.predict_proba([query])
        class_proba_tuples = list(predict_proba_result[0][1].items())
        return sorted(class_proba_tuples, key=lambda x: x[1], reverse=True)

    def evaluate(self, queries=None, label_set='test'):
        """Evaluates the trained classification model on the given test data

        Args:
            queries (list of ProcessedQuery): The labeled queries to use as test data. If none
                are provided, the test label set will be used.
            label_set (str): The label set to use for evaluation.

        Returns:
            ModelEvaluation: A ModelEvaluation object that contains evaluation results
        """
        if not self._model:
            logger.error('You must fit or load the model before running evaluate.')
            return

        queries, labels = self._get_queries_and_labels(queries, label_set=label_set)

        if not queries:
            logger.info('Could not evaluate model since no relevant examples were found. Make sure '
                        'the labeled queries for evaluation are placed in "test*" files alongside '
                        'the training data in your Workbench project.')
            return

        evaluation = self._model.evaluate(queries, labels)
        return evaluation

    def _get_model_config(self, loaded_config, **kwargs):
        """Updates the loaded configuration with runtime specified options, and creates a model
        configuration object with the final configuration dictionary. If an application config
        exists it should be passed in, if not the default config should be passed in.

        Returns:
            ModelConfig: The model configuration corresponding to the provided config name
        """
        try:
            # If all params required for model config were passed in, use kwargs
            return ModelConfig(**kwargs)
        except (TypeError, ValueError):
            # Use application specified or default config, customizing with provided kwargs
            model_config = loaded_config
            model_config.update(kwargs)

            # If a parameter selection grid was passed in at runtime, override params set in the
            # application specified or default config
            if kwargs.get('param_selection') and not kwargs.get('params'):
                model_config.pop('params', None)
        return ModelConfig(**model_config)

    def dump(self, model_path):
        """Persists the trained classification model to disk.

        Args:
            model_path (str): The location on disk where the model should be stored
        """
        # make directory if necessary
        folder = os.path.dirname(model_path)
        if not os.path.isdir(folder):
            os.makedirs(folder)

        joblib.dump(self._model, model_path)

        hash_path = model_path + '.hash'
        with open(hash_path, 'w') as hash_file:
            hash_file.write(self.hash)

        self.dirty = False

    def load(self, model_path):
        """Loads the trained classification model from disk

        Args:
            model_path (str): The location on disk where the model is stored
        """
        try:
            self._model = joblib.load(model_path)
        except (OSError, IOError):
            msg = 'Unable to load {}. Pickle at {!r} cannot be read.'
            raise ClassifierLoadError(msg.format(self.__class__.__name__, model_path))
        if self._model is not None:
            self._model.initialize_resources(self._resource_loader)
            self.config = ClassifierConfig.from_model_config(self._model.config)

        self.hash = self._load_hash(model_path)

        self.ready = True
        self.dirty = False

    @staticmethod
    def _load_hash(model_path):
        hash_path = model_path + '.hash'
        if not os.path.isfile(hash_path):
            return ''
        with open(hash_path, 'r') as hash_file:
            model_hash = hash_file.read()
        return model_hash

    @staticmethod
    def _build_query_tree(queries, raw=False):
        """Build a query tree from a list of ProcessedQueries. The tree is
        organized by domain then by intent.

        Args:
            queries (List): list of ProcessedQuery
        """
        query_tree = {}
        for query in queries:
            if query.domain not in query_tree:
                query_tree[query.domain] = {}
            if query.intent not in query_tree[query.domain]:
                query_tree[query.domain][query.intent] = []

            if raw:
                query_tree[query.domain][query.intent].append(markup.dump_query(query))
            else:
                query_tree[query.domain][query.intent].append(query)

        return query_tree

    @abstractmethod
    def _get_query_tree(self, queries=None, label_set='train', raw=False):
        """Returns the set of queries to train on

        Args:
            queries (list, optional): A list of ProcessedQuery objects, to
                train. If not specified, a label set will be loaded.
            label_set (list, optional): A label set to load. If not specified,
                the default training set will be loaded.
            raw (bool, optional): When True, raw query strings will be returned

        Returns:
            List: list of queries
        """
        raise NotImplementedError('Subclasses must implement this method')

    @abstractmethod
    def _get_queries_and_labels(self, queries=None, label_set='train'):
        """Returns the set of queries and their labels to train on

        Args:
            queries (list, optional): A list of ProcessedQuery objects, to
                train. If not specified, a label set will be loaded.
            label_set (list, optional): A label set to load. If not specified,
                the default training set will be loaded.
        """
        raise NotImplementedError('Subclasses must implement this method')

    @abstractmethod
    def _get_queries_and_labels_hash(self, queries=None, label_set='train'):
        """Returns a hashed string representing the labeled queries

        Args:
            queries (list, optional): A list of ProcessedQuery objects, to
                train. If not specified, a label set will be loaded.
            label_set (list, optional): A label set to load. If not specified,
                the default training set will be loaded.
        """
        raise NotImplementedError('Subclasses must implement this method')

    def _get_model_hash(self, model_config, queries=None, label_set='train'):
        """Returns a hash representing the inputs into the model

        Args:
            model_config (ModelConfig): The model configuration
            queries (list, optional): A list of ProcessedQuery objects, to
                train. If not specified, a label set will be loaded.
            label_set (list, optional): A label set to load. If not specified,
                the default training set will be loaded.

        Returns:
            str: The hash
        """
        hash_obj = hashlib.new('sha1')

        # Hash queries
        queries_hash = self._get_queries_and_labels_hash(queries=queries, label_set=label_set)

        hash_obj.update(queries_hash.encode('utf8'))

        # Hash config
        hash_obj.update(model_config.to_json().encode('utf8'))

        # Hash resources
        rsc_hash = hashlib.new('sha1')
        for resource in sorted(model_config.required_resources()):
            rsc_hash.update(self._resource_loader.hash_feature_resource(resource).encode('utf8'))
        hash_obj.update(rsc_hash.hexdigest().encode('utf8'))

        return hash_obj.hexdigest()

    def __repr__(self):
        msg = '<{} ready: {!r}, dirty: {!r}>'
        return msg.format(self.__class__.__name__, self.ready, self.dirty)
