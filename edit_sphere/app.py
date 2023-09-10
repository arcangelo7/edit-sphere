import json
import os
import urllib

import click
import requests
from flask import Flask, redirect, render_template, request, session, url_for
from flask_babel import Babel, refresh
from SPARQLWrapper import JSON, SPARQLWrapper

from config import Config
from edit_sphere.editor import Editor
from edit_sphere.filters import *

app = Flask(__name__)

app.config.from_object(Config)

babel = Babel()


with open("resources/context.json", "r") as config_file:
    context = json.load(config_file)["@context"]


dataset_endpoint = app.config["DATASET_ENDPOINT"]
provenance_endpoint = app.config["PROVENANCE_ENDPOINT"]
sparql = SPARQLWrapper(dataset_endpoint)

filter = Filter(context)

app.jinja_env.filters['human_readable_predicate'] = filter.human_readable_predicate
app.jinja_env.filters['split_ns'] = filter.split_ns

@app.route('/')
def index():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 100))
    offset = (page - 1) * per_page

    query = f"""
    SELECT DISTINCT ?subject WHERE {{
        ?subject ?predicate ?object.
    }} LIMIT {per_page} OFFSET {offset}
    """
    sparql.setQuery(query)
    sparql.setReturnFormat(JSON)
    subjects = sparql.query().convert().get("results", {}).get("bindings", [])
    return render_template('entities.html', subjects=subjects, page=page)

@app.route('/triples/<path:subject>')
def show_triples(subject):
    # Decodifica l'URL prima di usarlo nella query
    decoded_subject = urllib.parse.unquote(subject)
    query = f"""
    SELECT ?predicate ?object WHERE {{
        <{decoded_subject}> ?predicate ?object.
    }}
    """
    sparql.setQuery(query)
    sparql.setReturnFormat(JSON)
    triples = sparql.query().convert().get("results", {}).get("bindings", [])
    return render_template('triples.html', subject=decoded_subject, triples=triples)

@app.route('/update_triple', methods=['POST'])
def update_triple():
    subject = request.form.get('subject')
    predicate = request.form.get('predicate')
    old_value = request.form.get('old_value')
    new_value = request.form.get('new_value')
    editor = Editor(dataset_endpoint, provenance_endpoint, app.config['COUNTER_HANDLER'])
    editor.update(subject, predicate, old_value, new_value)    
    return redirect(url_for('show_triples', subject=subject))

@app.route('/delete_triple', methods=['POST'])
def delete_triple():
    subject = request.form.get('subject')
    predicate = request.form.get('predicate')
    object_value = request.form.get('object')
    editor = Editor(dataset_endpoint, provenance_endpoint, app.config['COUNTER_HANDLER'])
    editor.delete(subject, predicate, object_value)
    return redirect(url_for('show_triples', subject=subject))

@app.route('/search')
def search():
    subject = request.args.get('q')
    return redirect(url_for('show_triples', subject=subject))

@app.route('/set-language/<lang_code>')
def set_language(lang_code=None):
    session['lang'] = lang_code
    refresh()
    return redirect(request.referrer or url_for('index'))

@app.route('/endpoint')
def endpoint():
    return render_template('endpoint.html', dataset_endpoint=dataset_endpoint)

@app.route('/dataset-endpoint', methods=['POST'])
def sparql_proxy():
    query = request.form.get('query')
    response = requests.post(dataset_endpoint, data={'query': query}, headers={'Accept': 'application/sparql-results+json'})
    return response.content, response.status_code, {'Content-Type': 'application/sparql-results+json'}

@app.cli.group()
def translate():
    """Translation and localization commands."""
    pass

@translate.command()
def update():
    """Update all languages."""
    if os.system('pybabel extract -F babel/babel.cfg -k lazy_gettext -o babel/messages.pot .'):
        raise RuntimeError('extract command failed')
    if os.system('pybabel update -i babel/messages.pot -d babel/translations'):
        raise RuntimeError('update command failed')
    os.remove('babel/messages.pot')

@translate.command()
def compile():
    """Compile all languages."""
    if os.system('pybabel compile -d babel/translations'):
        raise RuntimeError('compile command failed')

@translate.command()
@click.argument('lang')
def init(lang):
    """Initialize a new language."""
    if os.system('pybabel extract -F babel/babel.cfg -k _l -o messages.pot .'):
        raise RuntimeError('extract command failed')
    if os.system(
            'pybabel init -i messages.pot -d babel/translations -l ' + lang):
        raise RuntimeError('init command failed')
    os.remove('messages.pot')

def get_locale():
    return session.get('lang', 'en')

babel.init_app(app=app, locale_selector=get_locale, default_translation_directories=app.config['BABEL_TRANSLATION_DIRECTORIES'])