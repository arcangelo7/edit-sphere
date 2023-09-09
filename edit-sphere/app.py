import json
import urllib

import yaml
from filters import *
from flask import Flask, redirect, render_template, request, url_for
from SPARQLWrapper import JSON, SPARQLWrapper

app = Flask(__name__)

with open("config.yaml", "r") as config_file:
    config = yaml.safe_load(config_file)

with open("resources/context.json", "r") as config_file:
    context = json.load(config_file)["@context"]

sparql = SPARQLWrapper(config["SPARQL_ENDPOINT"])

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

@app.route('/update_triple/<path:subject>/<path:predicate>', methods=['POST'])
def update_triple(subject, predicate):
    new_value = request.form.get('new_value')
    
    # Qui dovrai aggiungere il codice per aggiornare il valore nel tuo endpoint SPARQL.
    # Questo dipenderà dalla tua configurazione e dal tuo endpoint SPARQL.
    
    return redirect(url_for('show_triples', subject=subject))

if __name__ == '__main__':
    app.run(debug=True)