import os
import tarfile
import tempfile
import logging
import importlib
from distutils import dir_util

import yaml
import ulid

from h1st.model_repository.storage.s3 import S3Storage
from h1st.model_repository.storage.local import LocalStorage

SEP = "::"
logger = logging.getLogger(__name__)


class ModelSerDe:
    STATS_PATH = 'stats.joblib'
    METRICS_PATH = 'metrics.joblib'
    METAINFO_FILE = 'METAINFO.yaml'

    def _serialize_dict(self, d, path, dict_file):
        import joblib
        joblib.dump(d, path + '/%s' % dict_file)

    def _deserialize_dict(self, path, dict_file):
        import joblib
        return joblib.load(path + '/%s' % dict_file)

    def _get_model_type(self, model):
        import tensorflow
        import sklearn

        if isinstance(model, sklearn.base.BaseEstimator):
            return 'sklearn'
        if isinstance(model, tensorflow.keras.Model):
            return 'tensorflow-keras'
        if model is None:
            return 'custom'

    def _serialize_single_model(self, model, path, model_name='model'):
        model_type = self._get_model_type(model)

        if model_type == 'sklearn':
            # This is a sklearn model
            import joblib
            model_path = '%s.joblib' % model_name
            joblib.dump(model, path + '/%s' % model_path)
        elif model_type == 'tensorflow-keras':
            model_path = model_name
            os.makedirs(path + '/%s' % model_path, exist_ok=True)
            model.save_weights(path + '/%s/weights' % model_path)
        elif model_type == 'custom':
            model_path = model_name  # XXX
        else:
            raise ValueError('Unsupported model!!!')

        return model_type, model_path

    def _deserialize_single_model(self, model, path, model_type, model_path):
        if model_type == 'sklearn':
            # This is a sklearn model
            import joblib
            model = joblib.load(path + '/%s' % model_path)
            # print(str(type(model)))
        elif model_type == 'tensorflow-keras':
            model.load_weights(path + '/%s/weights' % model_path).expect_partial()
        elif model_type == 'custom':
            model = None

        return model

    def serialize(self, model, path):
        """
        Serialize a H1ST model's model property to disk.

        :param model: H1ST Model
        :param path: path to save models to
        """
        meta_info = {}

        if hasattr(model, 'metrics'):
            logger.info('Saving metrics property...')
            meta_info['metrics'] = self.METRICS_PATH
            self._serialize_dict(model.metrics, path, self.METRICS_PATH)

        if hasattr(model, 'stats'):
            logger.info('Saving stats property...')
            meta_info['stats'] = self.STATS_PATH
            self._serialize_dict(model.stats, path, self.STATS_PATH)

        if hasattr(model, 'model'):
            logger.info('Saving model property...')
            if type(model.model) == list:
                meta_info['models'] = []
                for i, model in enumerate(model.model):
                    model_type, model_path = self._serialize_single_model(model, path, 'model_%d' % i)
                    meta_info['models'].append({'model_type': model_type, 'model_path': model_path})
            elif type(model.model) == dict:
                meta_info['models'] = {}
                for k, model in model.model.items():
                    model_type, model_path = self._serialize_single_model(model, path, 'model_%s' % k)
                    meta_info['models'][k] = {'model_type': model_type, 'model_path': model_path}
            else:
                # this is a single model
                model_type, model_path = self._serialize_single_model(model.model, path)
                meta_info['models'] = [{'model_type': model_type, 'model_path': model_path}]

        if len(meta_info) == 0:
            logger.info('Model persistence currently supports only stats, model and metrics properties.')
            logger.info('Make sure you store stastistic in stats property, models in model property and model metrics in metrics one.')

        with open(os.path.join(path, self.METAINFO_FILE), 'w') as file:
            yaml.dump(meta_info, file)

    def deserialize(self, model, path):
        """
        Populate a H1ST model's model property with saved atomic models.

        :param model: H1ST Model
        :param path: path to model folder
        """
        # Read METAINFO
        with open(os.path.join(path, self.METAINFO_FILE), 'r') as file:
            meta_info = yaml.load(file, Loader=yaml.Loader)

        if 'metrics' in meta_info.keys():
            model.metrics = self._deserialize_dict(path, self.METRICS_PATH)

        if 'stats' in meta_info.keys():
            model.stats = self._deserialize_dict(path, self.STATS_PATH)

        if 'models' in meta_info.keys():
            model_infos = meta_info['models']
            org_model = getattr(model, 'model', None)  # original model object from Model class
            if type(model_infos) == list:
                if len(model_infos) == 1:
                    # Single model
                    model_info = model_infos[0]
                    # print(model_info)
                    model_type = model_info['model_type']
                    model_path = model_info['model_path']
                    model.model = self._deserialize_single_model(org_model, path, model_type, model_path)
                else:
                    # A list of models
                    model.model = []
                    org_model = org_model or [None for _ in range(len(model_infos))]
                    for i, model_info in enumerate(model_infos):
                        model_type = model_info['model_type']
                        model_path = model_info['model_path']
                        model.model.append(self._deserialize_single_model(org_model[i], path, model_type, model_path))

            elif type(model_infos) == dict:
                # A dict of models
                model.model = {}
                org_model = org_model or {k: None for k in model_infos.keys()}
                for model_name, model_info in model_infos.items():
                    model_type = model_info['model_type']
                    model_path = model_info['model_path']
                    model.model[model_name] = self._deserialize_single_model(org_model[model_name], path, model_type, model_path)
            else:
                raise ValueError('Not a valid H1ST Model METAINFO file!')


class ModelRepository:
    """
    Model repository allows user to persist and load model to different storage system.

    Model repository uses ``ModelSerDer`` to serialize a model into a temporary folder
    and then create a tar archive to store on storage. For loading, the repo retrieve
    the tar archive from the storage and then extract to temporary folder for restoring
    the model object.
    """

    _NAMESPACE = "_models"

    _DEFAULT_STORAGE = S3Storage

    def __init__(self, storage=None):
        if isinstance(storage, str) and "s3://" in storage:
            storage = storage.replace("s3://", "").strip("/") + "/"
            bucket, prefix = storage.split("/", 1)
            storage = S3Storage(bucket, prefix.strip("/"))
            self._NAMESPACE = ""
        elif isinstance(storage, str):  # local folder
            storage = LocalStorage(storage)
            self._NAMESPACE = ""

        self._storage = storage or ModelRepository._DEFAULT_STORAGE()
        self._serder = ModelSerDe()

    def persist(self, model, version=None):
        """
        Save a model to the model repository

        :param model: target model
        :param version: version name, leave blank for autogeneration
        :returns: model version
        """
        # assert isinstance(model, Model)
        # TODO: use version format: v_20200714-1203
        version = version or str(ulid.new())

        try:
            tmpdir = tempfile.mkdtemp()
            with tempfile.NamedTemporaryFile() as f:

                self._serder.serialize(model, tmpdir)
                _tar_create(f.name, tmpdir)
                f.flush()
                f.seek(0)

                self._storage.set_bytes(
                    self._get_key(model, version),
                    f.read(),
                )

                self._storage.set_obj(
                    self._get_key(model, 'latest'),
                    version,
                )

                model.version = version
        finally:
            dir_util.remove_tree(tmpdir)

        return version

    def load(self, model, version=None):
        """
        Restore the model from the model repository

        :param model: target model
        :param version: version name, leave blank to load the latest version
        """
        # assert isinstance(model, Model)
        if version is None:
            version = self._storage.get_obj(self._get_key(model, 'latest'))

        logger.info('Loading version %s ....' % version)

        try:
            tmpdir = tempfile.mkdtemp()
            with tempfile.NamedTemporaryFile(mode="wb") as f:
                f.write(self._storage.get_bytes(
                    self._get_key(model, version)
                ))
                f.flush()
                f.seek(0)

                _tar_extract(f.name, tmpdir)
                self._serder.deserialize(model, tmpdir)
                model.version = version
        finally:
            # We get error from Tensorflow telling that it could not find the folder
            # Unsuccessful TensorSliceReader constructor: Failed to get matching files on 
            # /var/folders/wb/40304xlx477cfjzbk386l2gr0000gn/T/tmpwcrvm2e2/model/weights: 
            # Not found: /var/folders/wb/40304xlx477cfjzbk386l2gr0000gn/T/tmpwcrvm2e2/model; No such file or directory [Op:RestoreV2]
            #
            # dir_util.remove_tree(tmpdir)

            # instead, register the function to clean it up when the interpreter quits
            import atexit

            def clean_tmpdir(tmpdir):
                # print('Clean up %s'  % tmpdir)
                dir_util.remove_tree(tmpdir)

            atexit.register(clean_tmpdir, tmpdir=tmpdir)

    def delete(self, model, version):
        """
        Delete a model from model repository

        :param model: model instance or the model class
        :param version: target version
        """
        # assert isinstance(model, Model) or isinstance(model, type)
        assert version != 'latest'  # magic key
        self._storage.delete(self._get_key(model, version))

    def download(self, model, version, path):
        """
        Download a model archive to local disk

        :param model: model instance or model class
        :param version: version name
        :param path: target folder to extract the model archive
        """
        with tempfile.NamedTemporaryFile(mode="wb") as f:
            f.write(self._storage.get_bytes(
                self._get_key(model, version)
            ))
            f.flush()
            f.seek(0)

            _tar_extract(f.name, path)

        return path

    # TODO: list all versions

    def _get_key(self, model, version):
        model_class = model if isinstance(model, type) else model.__class__
        model_name = model_class.__module__ + '.' + model_class.__name__

        key = f"{model_name}{SEP}{version}"

        if self._NAMESPACE:
            key = f"{self._NAMESPACE}{SEP}{key}"

        return key

    @classmethod
    def get_model_repo(cls, ref=None):
        """
        Retrieve the default model repository for the project

        :param ref: target model
        :returns: Model repository instance
        """
        if not hasattr(cls, 'MODEL_REPO'):
            repo_path = None
            if ref is not None:
                # root module
                root_module_name = ref.__class__.__module__.split('.')[0]

                try:
                    module = importlib.import_module(root_module_name + ".config")
                    repo_path = getattr(module, 'MODEL_REPO_PATH', None)
                except ModuleNotFoundError:
                    repo_path = None

            if not repo_path:
                raise RuntimeError('Please set MODEL_REPO_PATH in config.py')

            setattr(cls, 'MODEL_REPO', ModelRepository(storage=repo_path))

        return getattr(cls, 'MODEL_REPO')


def _tar_create(target, source):
    """
    Helper function to create a tar archive
    """
    with tarfile.open(target, "w:gz") as tf:
        tf.add(source, arcname='', recursive=True)

    return target


def _tar_extract(source, target):
    """
    Helper function to extract a tar archive
    """
    with tarfile.open(source) as tf:
        tf.extractall(target)
