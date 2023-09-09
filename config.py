import os
from rdflib_ocdm.counter_handler.sqlite_counter_handler import SqliteCounterHandler

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

class Config(object):
    SECRET_KEY = 'adoiugwoad7y78agdlauwvdliu'
    DATASET_ENDPOINT = 'http://192.168.56.1:9999/blazegraph/sparql'
    PROVENANCE_ENDPOINT = 'http://192.168.56.1:19999/blazegraph/sparql'
    COUNTER_HANDLER = SqliteCounterHandler(os.path.join(BASE_DIR, 'bear_a_counter_handler.db'))
    LANGUAGES = ['en', 'it']
    BABEL_TRANSLATION_DIRECTORIES = os.path.join(BASE_DIR, 'babel', 'translations')