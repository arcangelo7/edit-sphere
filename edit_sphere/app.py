import datetime
import json
import os
import urllib
from collections import defaultdict
from datetime import timezone

import click
import requests
import validators
from flask import (Flask, flash, redirect, render_template, request, session,
                   url_for)
from flask_babel import Babel, gettext, refresh
from rdflib import RDF, XSD, Graph, Literal, URIRef
from rdflib.plugins.sparql import prepareQuery
from rdflib.plugins.sparql.algebra import translateUpdate
from rdflib.plugins.sparql.parser import parseUpdate
from SPARQLWrapper import JSON, XML, SPARQLWrapper
from time_agnostic_library.agnostic_entity import (
    AgnosticEntity, _filter_timestamps_by_interval)
from time_agnostic_library.prov_entity import ProvEntity
from time_agnostic_library.sparql import Sparql
from time_agnostic_library.support import convert_to_datetime

from config import Config
from edit_sphere.editor import Editor
from edit_sphere.filters import *
from edit_sphere.forms import *
from resources.datatypes import DATATYPE_MAPPING

app = Flask(__name__)

app.config.from_object(Config)

babel = Babel()


with open("resources/context.json", "r") as config_file:
    context = json.load(config_file)["@context"]


dataset_endpoint = app.config["DATASET_ENDPOINT"]
provenance_endpoint = app.config["PROVENANCE_ENDPOINT"]
sparql = SPARQLWrapper(dataset_endpoint)
change_tracking_config = app.config["CHANGE_TRACKING_CONFIG"]

shacl_path = app.config["SHACL_PATH"]
shacl = None
if shacl_path:
    if os.path.exists(shacl_path):
        shacl = Graph()
        shacl.parse(source=app.config["SHACL_PATH"], format="turtle")

filter = Filter(context)

app.jinja_env.filters['human_readable_predicate'] = filter.human_readable_predicate
app.jinja_env.filters['format_datetime'] = filter.human_readable_datetime
app.jinja_env.filters['split_ns'] = filter.split_ns

@app.route('/')
def index():
    return render_template('index.jinja')

@app.route('/catalogue')
def catalogue():
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
    return render_template('entities.jinja', subjects=subjects, page=page)

@app.route('/triples/<path:subject>')
def show_triples(subject):
    # Decodifica l'URL prima di usarlo nella query
    decoded_subject = urllib.parse.unquote(subject)
    agnostic_entity = AgnosticEntity(res=decoded_subject, config_path=change_tracking_config)
    history, provenance = agnostic_entity.get_history(include_prov_metadata=True)
    query = f"""
    SELECT ?predicate ?object WHERE {{
        <{decoded_subject}> ?predicate ?object.
    }}
    """
    sparql.setQuery(query)
    sparql.setReturnFormat(JSON)
    triples = sparql.query().convert().get("results", {}).get("bindings", [])
    can_be_added, can_be_deleted, datatypes = get_valid_predicates(triples)
    update_form = UpdateTripleForm()
    if can_be_added:
        create_form = CreateTripleFormWithSelect()
        create_form.predicate.choices = [(p, filter.human_readable_predicate(p)) for p in can_be_added]
    else:
        create_form = CreateTripleFormWithInput()
    return render_template('triples.jinja', subject=decoded_subject, triples=triples, history=history, can_be_added=can_be_added, can_be_deleted=can_be_deleted, datatypes=datatypes, update_form=update_form, create_form=create_form)

@app.route('/update_triple', methods=['POST'])
def update_triple():
    subject = request.form.get('subject')
    predicate = request.form.get('predicate')
    old_value = request.form.get('old_value')
    new_value = request.form.get('new_value')
    if shacl:
        new_value, old_value, report_text = validate_new_triple(subject, predicate, new_value, old_value)
        if new_value is None:
            flash(report_text)
            return redirect(url_for('show_triples', subject=subject))
    else:
        new_value = URIRef(new_value) if validators.url(new_value) else Literal(new_value)
        old_value = URIRef(old_value) if validators.url(old_value) else Literal(old_value)
    editor = Editor(dataset_endpoint, provenance_endpoint, app.config['COUNTER_HANDLER'], app.config['RESPONSIBLE_AGENT'])
    editor.update(URIRef(subject), URIRef(predicate), old_value, new_value)    
    return redirect(url_for('show_triples', subject=subject))

@app.route('/delete_triple', methods=['POST'])
def delete_triple():
    subject = request.form.get('subject')
    predicate = request.form.get('predicate')
    object_value = request.form.get('object')
    if shacl:
        data_graph = fetch_data_graph_for_subject(subject)
        _, can_be_deleted, _ = get_valid_predicates(list(data_graph.triples((None, None, None))))
        if predicate not in can_be_deleted:
            flash(gettext('This property cannot be deleted'))
            return redirect(url_for('show_triples', subject=subject))
    editor = Editor(dataset_endpoint, provenance_endpoint, app.config['COUNTER_HANDLER'], app.config['RESPONSIBLE_AGENT'])
    editor.delete(subject, predicate, object_value)
    return redirect(url_for('show_triples', subject=subject))

@app.route('/add_triple', methods=['POST'])
def add_triple():
    subject = request.form.get('subject')
    predicate = request.form.get('predicate')
    object_value = request.form.get('object')
    if shacl:
        data_graph = fetch_data_graph_for_subject(subject)
        can_be_added, _, _ = get_valid_predicates(list(data_graph.triples((None, None, None))))
        if predicate not in can_be_added and URIRef(predicate) in data_graph.predicates():
            flash(gettext('This resource cannot have any other %(predicate)s properties', predicate=filter.human_readable_predicate(predicate)))
            return redirect(url_for('show_triples', subject=subject))
        object_value, _, report_text = validate_new_triple(subject, predicate, object_value)
        if object_value is None:
            flash(report_text)
            return redirect(url_for('show_triples', subject=subject))
    else:
        object_value = URIRef(object_value) if validators.url(object_value) else Literal(object_value)
    editor = Editor(dataset_endpoint, provenance_endpoint, app.config['COUNTER_HANDLER'], app.config['RESPONSIBLE_AGENT'])
    editor.create(URIRef(subject), URIRef(predicate), object_value)
    return redirect(url_for('show_triples', subject=subject))

def validate_new_triple(subject, predicate, new_value, old_value = None):
    data_graph = fetch_data_graph_for_subject(subject)
    s_types = [triple[2] for triple in data_graph.triples((URIRef(subject), RDF.type, None))]
    if old_value is not None:
        old_value = [triple[2] for triple in data_graph.triples((URIRef(subject), URIRef(predicate), None)) if str(triple[2]) == str(old_value)][0]
    query = f"""
        PREFIX sh: <http://www.w3.org/ns/shacl#>
        SELECT DISTINCT ?property ?datatype ?a_class ?classIn WHERE {{
            ?shape sh:targetClass ?type ;
                sh:property ?property .
            VALUES ?type {{<{'> <'.join(s_types)}>}}
            ?property sh:path <{predicate}> .
            OPTIONAL {{?property sh:datatype ?datatype .}}
            OPTIONAL {{?property sh:class ?a_class .}}
            OPTIONAL {{
                ?property sh:or ?orList .
                ?orList rdf:rest*/rdf:first ?orConstraint .
                ?orConstraint sh:datatype ?datatype .
                OPTIONAL {{?orConstraint sh:class ?class .}}
            }}
            OPTIONAL {{
                ?property sh:classIn ?classInList .
                ?classInList rdf:rest*/rdf:first ?classIn .
            }}
        }}
    """
    results = shacl.query(query)
    property_exists = [row.property for row in results]
    if not property_exists:
        return None, old_value, gettext('The property %(predicate)s is not allowed for resources of type %(s_type)s', predicate=filter.human_readable_predicate(predicate), s_type=filter.human_readable_predicate(s_types[0]))
    datatypes = [row.datatype for row in results]
    classes = [row.a_class for row in results if row.a_class]
    classes.extend([row.classIn for row in results if row.classIn])
    if classes:
        if not validators.url(new_value):
            return None, old_value, gettext('<code>%(new_value)s</code> is not a valid value. The <code>%(property)s</code> property requires values of type %(o_types)s', new_value=filter.human_readable_predicate(new_value), property=filter.human_readable_predicate(predicate), o_types=', '.join([f'<code>{filter.human_readable_predicate(o_class)}</code>' for o_class in classes]))
        valid_value = convert_to_matching_class(new_value, classes)
        if valid_value is None:
            return None, old_value, gettext('<code>%(new_value)s</code> is not a valid value. The <code>%(property)s</code> property requires values of type %(o_types)s', new_value=filter.human_readable_predicate(new_value), property=filter.human_readable_predicate(predicate), o_types=', '.join([f'<code>{filter.human_readable_predicate(o_class)}</code>' for o_class in classes]))
        return valid_value, old_value, ''
    elif datatypes:
        valid_value = convert_to_matching_literal(new_value, datatypes)
        if valid_value is None:
            return None, old_value, gettext('<code>%(new_value)s</code> is not a valid value. The <code>%(property)s</code> property requires values of type %(o_types)s', new_value=filter.human_readable_predicate(new_value), property=filter.human_readable_predicate(predicate), o_types=', '.join([f'<code>{filter.human_readable_predicate(datatype)}</code>' for datatype in datatypes]))
        return valid_value, old_value, ''
    valid_value = URIRef(new_value) if validators.url(new_value) else Literal(new_value)
    return valid_value, old_value, ''

def fetch_data_graph_for_subject(subject_uri):
    """
    Fetch all triples associated with subject.
    """
    query_str = f'''
        CONSTRUCT {{
            <{subject_uri}> ?p ?o .
        }}
        WHERE {{
            <{subject_uri}> ?p ?o .
        }}
    '''
    sparql.setQuery(query_str)
    sparql.setReturnFormat(XML)
    result = sparql.queryAndConvert()
    return result

def fetch_data_graph_for_subject_recursively(subject_uri):
    """
    Fetch all triples associated with subject and all triples of all the entities that the subject points to.
    """
    query_str = f'''
        PREFIX eea: <https://jobu_tupaki/>
        CONSTRUCT {{
            ?s ?p ?o .
        }}
        WHERE {{
            {{
                <{subject_uri}> ?p ?o .
                ?s ?p ?o .
            }} UNION {{
                <{subject_uri}> (<eea:everything_everywhere_allatonce>|!<eea:everything_everywhere_allatonce>)* ?s.
                ?s ?p ?o. 
            }}
        }}
    '''
    sparql.setQuery(query_str)
    sparql.setReturnFormat(XML)
    result = sparql.queryAndConvert()
    return result

def convert_to_matching_class(object_value, classes):
    data_graph = fetch_data_graph_for_subject(object_value)
    o_types = {c[2] for c in data_graph.triples((URIRef(object_value), RDF.type, None))}
    if o_types.intersection(classes):
        return URIRef(object_value)

def convert_to_matching_literal(object_value, datatypes):
    for datatype in datatypes:
        validation_func = next((d[1] for d in DATATYPE_MAPPING if d[0] == datatype), None)
        if validation_func is None:
            return Literal(object_value)
        is_valid_datatype = validation_func(object_value)
        if is_valid_datatype:
            return Literal(object_value, datatype=datatype)

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
    return render_template('endpoint.jinja', dataset_endpoint=dataset_endpoint)

@app.route('/dataset-endpoint', methods=['POST'])
def sparql_proxy():
    query = request.form.get('query')
    response = requests.post(dataset_endpoint, data={'query': query}, headers={'Accept': 'application/sparql-results+json'})
    return response.content, response.status_code, {'Content-Type': 'application/sparql-results+json'}

from datetime import datetime


@app.route('/entity-history/<path:entity_uri>')
def entity_history(entity_uri):
    agnostic_entity = AgnosticEntity(res=entity_uri, config_path=change_tracking_config)
    history, provenance = agnostic_entity.get_history(include_prov_metadata=True)

    # Trasforma i dati in formato TimelineJS
    events = []
    sorted_metadata = sorted(provenance[entity_uri].items(), key=lambda x: x[1]['generatedAtTime'])  # Ordina gli eventi per data

    for i, (snapshot_uri, metadata) in enumerate(sorted_metadata):
        date = datetime.fromisoformat(metadata['generatedAtTime'])
        responsible_agent = f"<a href='{metadata['wasAttributedTo']}' alt='{gettext('Link to the responsible agent description')} target='_blank'>{metadata['wasAttributedTo']}</a>" if validators.url(metadata['wasAttributedTo']) else metadata['wasAttributedTo']
        primary_source = gettext('Unknown') if not metadata['hadPrimarySource'] else f"<a href='{metadata['hadPrimarySource']}' alt='{gettext('Link to the primary source description')} target='_blank'>{metadata['hadPrimarySource']}</a>" if validators.url(metadata['hadPrimarySource']) else metadata['hadPrimarySource']
        modifications = metadata['hasUpdateQuery']
        modification_text = ""
        if modifications:
            modifications = parse_sparql_update(modifications)
            for mod_type, triples in modifications.items():
                for triple in triples:
                    modification_text += f"<p><strong>{mod_type}</strong>: "
                    if filter.human_readable_predicate(triple[1]) != triple[1]:
                        href = f"{filter.split_ns(triple[1])[0][:-1]}#{triple[1]}"
                        alt = gettext('Definition of the property') + ' ' + triple[1]
                        modification_text += f"<a href='{href}' alt={alt} target='_blank' title='{triple[1]}'>{filter.human_readable_predicate(triple[1])}</a>"
                    else:
                        modification_text += triple[1]
                    modification_text += ' '
                    if filter.human_readable_predicate(triple[2]) != triple[2]:
                        href = f"{filter.split_ns(triple[2])[0][:-1]}#{triple[2]}"
                        alt = gettext('Definition of the property') + ' ' + triple[2]
                        modification_text += f"<a href='{href}' alt={alt} target='_blank' title='{triple[1]}'>{filter.human_readable_predicate(triple[2])}</a>"
                    else:
                        modification_text += triple[2]
                    modification_text += '</p>'
        event = {
            "start_date": {
                "year": date.year,
                "month": date.month,
                "day": date.day,
                "hour": date.hour,
                "minute": date.minute,
                "second": date.second
            },
            "text": {
                "headline": gettext('Snapshot') + ' ' + str(i + 1),
                "text": f"""
                    <p><strong>""" + gettext('Responsible agent') + f"""</strong>: {responsible_agent}</p>
                    <p><strong>""" + gettext('Primary source') + f"""</strong>: {primary_source}</p>
                    <p><strong>""" + gettext('Description') + f"""</strong>: {metadata['description']}</p>
                    {modification_text}"""
            },
            "autolink": False
        }

        # Imposta l'end_date sull'evento successivo, se esiste
        if i + 1 < len(sorted_metadata):
            next_date = datetime.fromisoformat(sorted_metadata[i + 1][1]['generatedAtTime'])
            event["end_date"] = {
                "year": next_date.year,
                "month": next_date.month,
                "day": next_date.day,
                "hour": next_date.hour,
                "minute": next_date.minute,
                "second": next_date.second
            }
        else:
            # Se è l'ultimo evento, imposta l'end_date al momento attuale
            now = datetime.now()
            event["end_date"] = {
                "year": now.year,
                "month": now.month,
                "day": now.day,
                "hour": now.hour,
                "minute": now.minute,
                "second": now.second
            }

        view_version_button = f"<button><a href='/entity-version/{entity_uri}/{metadata['generatedAtTime']}' alt='{gettext('Materialize snapshot')} {i+1}' target='_self'>" + gettext('View version') + "</a></button>"
        event["text"]["text"] += f"<br>{view_version_button}"
        events.append(event)

    timeline_data = {
        "title": {
            "text": {
                "headline": gettext('Version history for') + ' ' + entity_uri
            }
        },
        "events": events
    }

    return render_template('entity_history.jinja', entity_uri=entity_uri, timeline_data=timeline_data)

@app.route('/entity-version/<path:entity_uri>/<timestamp>')
def entity_version(entity_uri, timestamp):
    agnostic_entity = AgnosticEntity(res=entity_uri, config_path=change_tracking_config)
    history, metadata, other_snapshots_metadata = agnostic_entity.get_state_at_time(time=(None, timestamp), include_prov_metadata=True)
    timestamp_dt = datetime.fromisoformat(timestamp)
    if not timestamp_dt.tzinfo:
        timestamp_dt = timestamp_dt.replace(tzinfo=timezone.utc)
    history = {k: v for k, v in history.items()}
    for key, value in metadata.items():
        value['generatedAtTime'] = datetime.fromisoformat(value['generatedAtTime']).astimezone(timezone.utc).isoformat()

    closest_timestamp = min(history.keys(), key=lambda t: abs(datetime.fromisoformat(t).astimezone(timezone.utc) - timestamp_dt))
    version: Graph = history[closest_timestamp]
    triples = list(version.triples((None, None, None)))
    
    sorted_snapshots = sorted(other_snapshots_metadata.items(), key=lambda x: x[1]['generatedAtTime'])
    next_snapshot_timestamp = None
    prev_snapshot_timestamp = None
    for snapshot_uri, meta in sorted_snapshots:
        if meta['generatedAtTime'] >= timestamp:
            next_snapshot_timestamp = meta['generatedAtTime']
            break
    for snapshot_uri, meta in reversed(sorted_snapshots):
        if meta['generatedAtTime'] < timestamp:
            prev_snapshot_timestamp = meta['generatedAtTime']
            break
    if not prev_snapshot_timestamp:
        sorted_metadata = sorted(metadata.items(), key=lambda x: x[1]['generatedAtTime'], reverse=True)
        for snapshot_uri, meta in sorted_metadata:
            if meta['generatedAtTime'] < timestamp and not datetime.fromisoformat(meta['generatedAtTime']).replace(tzinfo=None) == datetime.fromisoformat(closest_timestamp):
                prev_snapshot_timestamp = meta['generatedAtTime']
                break
    closest_metadata_key = min(metadata.keys(), key=lambda k: abs(datetime.fromisoformat(metadata[k]['generatedAtTime']).astimezone(timezone.utc) - timestamp_dt))
    closest_metadata = {closest_metadata_key: metadata[closest_metadata_key]}
    if closest_metadata[closest_metadata_key]['hasUpdateQuery']:
        sparql_query = closest_metadata[closest_metadata_key]['hasUpdateQuery']
        modifications = parse_sparql_update(sparql_query)
    else:
        modifications = None

    return render_template('entity_version.jinja', subject=entity_uri, triples=triples, metadata=closest_metadata, timestamp=closest_timestamp, next_snapshot_timestamp=next_snapshot_timestamp, prev_snapshot_timestamp=prev_snapshot_timestamp, modifications=modifications)

@app.route('/restore-version/<path:entity_uri>/<timestamp>', methods=['POST'])
def restore_version(entity_uri, timestamp):
    query_snapshots = f"""
        SELECT ?time ?updateQuery
        WHERE {{
            ?snapshot <{ProvEntity.iri_specialization_of}> <{entity_uri}>;
                <{ProvEntity.iri_generated_at_time}> ?time
            OPTIONAL {{
                ?snapshot <{ProvEntity.iri_has_update_query}> ?updateQuery.
            }}
        }}
    """
    results = list(Sparql(query_snapshots, config_path=change_tracking_config).run_select_query())
    results.sort(key=lambda x:convert_to_datetime(x[0]), reverse=True)
    relevant_results = _filter_timestamps_by_interval((timestamp, timestamp), results, time_index=0)
    agnostic_entity = AgnosticEntity(res=entity_uri, config_path=change_tracking_config)
    entity_cg = agnostic_entity._query_dataset()
    sum_update_queries = ""
    for relevant_result in relevant_results:
        for result in results:
            if result[1]:
                if convert_to_datetime(result[0]) > convert_to_datetime(relevant_result[0]):
                    sum_update_queries += (result[1]) +  ";"
    inverted_query = invert_sparql_update(sum_update_queries)
    execute_sparql_update(inverted_query)
    query = f"""
        SELECT ?predicate ?object WHERE {{
            <{entity_uri}> ?predicate ?object.
        }}
    """
    sparql.setQuery(query)
    sparql.setReturnFormat(JSON)
    triples = sparql.query().convert().get("results", {}).get("bindings", [])
    return render_template('triples.jinja', subject=entity_uri, triples=triples, history={entity_uri: True})

def parse_sparql_update(query):
    parsed = parseUpdate(query)
    translated = translateUpdate(parsed).algebra
    modifications = {gettext('Deletions'): [], gettext('Additions'): []}

    for operation in translated:
        if operation.name == "DeleteData":
            modifications[gettext('Deletions')].extend(operation.triples)
        elif operation.name == "InsertData":
            modifications[gettext('Additions')].extend(operation.triples)

    return modifications

def invert_sparql_update(sparql_query: str) -> str:
    inverted_query = sparql_query.replace('INSERT', 'TEMP_REPLACE').replace('DELETE', 'INSERT').replace('TEMP_REPLACE', 'DELETE')
    return inverted_query

def execute_sparql_update(sparql_query: str):
    editor = Editor(dataset_endpoint, provenance_endpoint, app.config['COUNTER_HANDLER'], app.config['RESPONSIBLE_AGENT'])
    editor.execute(sparql_query)

def prioritize_datatype(datatypes):
    for datatype in DATATYPE_MAPPING:
        if datatype[0] in datatypes:
            return datatype[0]
    return DATATYPE_MAPPING[0][0]

def get_valid_predicates(triples):
    def extract_type(triple):
        if 'object' in triple:
            if 'predicate' in triple and triple['predicate']['value'] == str(RDF.type):
                return triple['object']['value']
        elif triple[1] == RDF.type:
            return triple[2]
        return None
    existing_predicates = [triple['predicate']['value'] if 'predicate' in triple else triple[1] for triple in triples]
    predicate_counts = {predicate: existing_predicates.count(predicate) for predicate in set(existing_predicates)}
    if not shacl:
        return None, None, None
    s_types = [extract_type(triple) for triple in triples if extract_type(triple) is not None]
    if not s_types:
        return None, None, None
    query = prepareQuery(f"""
        SELECT ?predicate ?datatype ?maxCount ?minCount WHERE {{
            ?shape sh:targetClass ?type ;
                   sh:property ?property .
            VALUES ?type {{<{'> <'.join(s_types)}>}}
            ?property sh:path ?predicate .
            OPTIONAL {{?property sh:datatype ?datatype .}}
            OPTIONAL {{?property sh:maxCount ?maxCount .}}
            OPTIONAL {{?property sh:minCount ?minCount .}}
            OPTIONAL {{
                ?property sh:or ?orList .
                ?orList rdf:rest*/rdf:first ?orConstraint .
                ?orConstraint sh:datatype ?datatype .
            }}
            FILTER (isURI(?predicate))
        }}
    """, initNs={"sh": "http://www.w3.org/ns/shacl#"})
    results = shacl.query(query)
    valid_predicates = [{row.predicate: {"min": (None if row.minCount is None else str(row.minCount)), 
                                               "max": (None if row.maxCount is None else str(row.maxCount))}} 
                        for row in results]
    can_be_added = list({
        str(predicate) for valid_predicate in valid_predicates for predicate, ranges in valid_predicate.items()
        if not (ranges["max"] is not None and int(ranges["max"]) <= predicate_counts.get(str(predicate), 0))
    })
    can_be_deleted = list({
        str(predicate) for valid_predicate in valid_predicates for predicate, ranges in valid_predicate.items()
        if not (ranges["min"] is not None and int(ranges["min"]) == predicate_counts.get(str(predicate), 0))
    })
    datatypes = defaultdict(list)
    for row in results:
        if row.datatype:
            datatypes[str(row.predicate)].append(row.datatype)
        else:
            datatypes[str(row.predicate)].append(XSD.string)
    return can_be_added, can_be_deleted, dict(datatypes)

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