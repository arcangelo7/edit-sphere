import json
import os
import urllib
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone

import click
import requests
import validators
import yaml
from config import Config
from edit_sphere.editor import Editor
from edit_sphere.filters import *
from edit_sphere.forms import *
from flask import (Flask, abort, flash, redirect, render_template, request,
                   session, url_for)
from flask_babel import Babel, gettext, refresh
from rdflib import RDF, XSD, Graph, Literal, URIRef
from rdflib.plugins.sparql import prepareQuery
from rdflib.plugins.sparql.algebra import translateQuery, translateUpdate
from rdflib.plugins.sparql.parser import parseQuery, parseUpdate
from resources.datatypes import DATATYPE_MAPPING
from SPARQLWrapper import JSON, RDFXML, XML, SPARQLWrapper
from time_agnostic_library.agnostic_entity import (
    AgnosticEntity, _filter_timestamps_by_interval)
from time_agnostic_library.prov_entity import ProvEntity
from time_agnostic_library.sparql import Sparql
from time_agnostic_library.support import convert_to_datetime

app = Flask(__name__)

app.config.from_object(Config)

babel = Babel()


with open("resources/context.json", "r") as config_file:
    context = json.load(config_file)["@context"]

display_rules_path = app.config["DISPLAY_RULES_PATH"]
display_rules = None
if display_rules_path:
    with open(display_rules_path, 'r') as f:
        display_rules = yaml.safe_load(f)

dataset_endpoint = app.config["DATASET_ENDPOINT"]
provenance_endpoint = app.config["PROVENANCE_ENDPOINT"]
sparql = SPARQLWrapper(dataset_endpoint)
provenance_sparql = SPARQLWrapper(provenance_endpoint)
change_tracking_config = app.config["CHANGE_TRACKING_CONFIG"]

shacl_path = app.config["SHACL_PATH"]
shacl = None
if shacl_path:
    if os.path.exists(shacl_path):
        shacl = Graph()
        shacl.parse(source=app.config["SHACL_PATH"], format="turtle")

filter = Filter(context, display_rules)

app.jinja_env.filters['human_readable_predicate'] = filter.human_readable_predicate
app.jinja_env.filters['human_readable_primary_source'] = filter.human_readable_primary_source
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
    decoded_subject = urllib.parse.unquote(subject)
    agnostic_entity = AgnosticEntity(res=decoded_subject, config_path=change_tracking_config)
    history, _ = agnostic_entity.get_history(include_prov_metadata=True)

    g = Graph()
    query = f"SELECT ?predicate ?object WHERE {{ <{decoded_subject}> ?predicate ?object. }}"
    sparql.setQuery(query)
    sparql.setReturnFormat(JSON)
    triples = sparql.query().convert().get("results", {}).get("bindings", [])
    for triple in triples:
        value = Literal(triple['object']['value'], datatype=URIRef(triple['object']['datatype'])) if triple['object']['type'] == 'literal' and 'datatype' in triple['object'] else Literal(triple['object']['value'], datatype=XSD.string) if triple['object']['type'] == 'literal' else URIRef(triple['object']['value'])
        g.add((URIRef(decoded_subject), URIRef(triple['predicate']['value']), value))
    triples = list(g.triples((None, None, None)))

    can_be_added, can_be_deleted, datatypes, mandatory_values, optional_values, subject_classes = get_valid_predicates(triples)

    if display_rules:
        grouped_triples, relevant_properties = get_grouped_triples(subject, triples, subject_classes)
    else:
        grouped_triples = dict()
        for triple in triples:
            grouped_triples.setdefault((triple[1], filter.human_readable_predicate(triple[1], subject_classes, True), False), []).append(triple)
        relevant_properties = set(triple[1] for triple in triples)
    triples = [triple for triple in triples if triple[1] in relevant_properties]
    can_be_added = [uri for uri in can_be_added if uri in relevant_properties]
    can_be_deleted = [uri for uri in can_be_deleted if uri in relevant_properties]

    update_form = UpdateTripleForm()
    create_form = CreateTripleFormWithSelect() if can_be_added else CreateTripleFormWithInput()
    if can_be_added:
        create_form.predicate.choices = [(p, filter.human_readable_predicate(p, subject_classes)) for p in can_be_added]
    return render_template('triples.jinja', subject=decoded_subject, history=history, can_be_added=can_be_added, can_be_deleted=can_be_deleted, datatypes=datatypes, update_form=update_form, create_form=create_form, mandatory_values=mandatory_values, optional_values=optional_values, shacl=True if shacl else False, grouped_triples=grouped_triples, subject_classes=subject_classes)

@app.route('/update_triple', methods=['POST'])
def update_triple():
    subject = request.form.get('subject')
    predicate = request.form.get('predicate')
    old_value = request.form.get('old_value')
    new_value = request.form.get('new_value')
    new_value, old_value, report_text = validate_new_triple(subject, predicate, new_value, old_value)
    if shacl:
        if new_value is None:
            flash(report_text)
            return redirect(url_for('show_triples', subject=subject))
    else:
        new_value = Literal(new_value, datatype=old_value.datatype) if old_value.datatype and isinstance(old_value, Literal) else Literal(new_value, datatype=XSD.string) if isinstance(old_value, Literal) else URIRef(new_value)
    editor = Editor(dataset_endpoint, provenance_endpoint, app.config['COUNTER_HANDLER'], app.config['RESPONSIBLE_AGENT'], app.config['PRIMARY_SOURCE'], app.config['DATASET_GENERATION_TIME'])
    editor.update(URIRef(subject), URIRef(predicate), old_value, new_value)    
    return redirect(url_for('show_triples', subject=subject))

@app.route('/delete_triple', methods=['POST'])
def delete_triple():
    subject = request.form.get('subject')
    predicate = request.form.get('predicate')
    object_value = request.form.get('object')
    if shacl:
        data_graph = fetch_data_graph_for_subject(subject)
        _, can_be_deleted, _, _, _, _ = get_valid_predicates(list(data_graph.triples((URIRef(subject), None, None))))
        if predicate not in can_be_deleted:
            flash(gettext('This property cannot be deleted'))
            return redirect(url_for('show_triples', subject=subject))
    editor = Editor(dataset_endpoint, provenance_endpoint, app.config['COUNTER_HANDLER'], app.config['RESPONSIBLE_AGENT'], app.config['PRIMARY_SOURCE'], app.config['DATASET_GENERATION_TIME'])
    editor.delete(subject, predicate, object_value)
    return redirect(url_for('show_triples', subject=subject))

@app.route('/add_triple', methods=['POST'])
def add_triple():
    subject = request.form.get('subject')
    predicate = request.form.get('predicate')
    object_value = request.form.get('object')
    object_value, _, report_text = validate_new_triple(subject, predicate, object_value)
    if shacl:
        data_graph = fetch_data_graph_for_subject(subject)
        can_be_added, _, _, _, _, s_types = get_valid_predicates(list(data_graph.triples((None, None, None))))
        if predicate not in can_be_added and URIRef(predicate) in data_graph.predicates():
            flash(gettext('This resource cannot have any other %(predicate)s properties', predicate=filter.human_readable_predicate(predicate, s_types)))
            return redirect(url_for('show_triples', subject=subject))
        if object_value is None:
            flash(report_text)
            return redirect(url_for('show_triples', subject=subject))
    else:
        object_value = URIRef(object_value) if validators.url(object_value) else Literal(object_value, datatype=XSD.string)
    editor = Editor(dataset_endpoint, provenance_endpoint, app.config['COUNTER_HANDLER'], app.config['RESPONSIBLE_AGENT'], app.config['PRIMARY_SOURCE'], app.config['DATASET_GENERATION_TIME'])
    editor.create(URIRef(subject), URIRef(predicate), object_value)
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
    return render_template('endpoint.jinja', dataset_endpoint=dataset_endpoint)

@app.route('/dataset-endpoint', methods=['POST'])
def sparql_proxy():
    query = request.form.get('query')
    response = requests.post(dataset_endpoint, data={'query': query}, headers={'Accept': 'application/sparql-results+json'})
    return response.content, response.status_code, {'Content-Type': 'application/sparql-results+json'}

@app.route('/entity-history/<path:entity_uri>')
def entity_history(entity_uri):
    agnostic_entity = AgnosticEntity(res=entity_uri, config_path=change_tracking_config)
    history, provenance = agnostic_entity.get_history(include_prov_metadata=True)
    subject_classes = [subject_class[2] for subject_class in list(history[entity_uri].values())[0].triples((URIRef(entity_uri), RDF.type, None))]
    # Trasforma i dati in formato TimelineJS useless
    events = []
    sorted_metadata = sorted(provenance[entity_uri].items(), key=lambda x: x[1]['generatedAtTime'])  # Ordina gli eventi per data

    for i, (snapshot_uri, metadata) in enumerate(sorted_metadata):
        date = datetime.fromisoformat(metadata['generatedAtTime'])
        responsible_agent = f"<a href='{metadata['wasAttributedTo']}' alt='{gettext('Link to the responsible agent description')} target='_blank'>{metadata['wasAttributedTo']}</a>" if validators.url(metadata['wasAttributedTo']) else metadata['wasAttributedTo']
        primary_source = filter.human_readable_primary_source(metadata['hadPrimarySource'])
        modifications = metadata['hasUpdateQuery']
        modification_text = ""
        if modifications:
            modifications = parse_sparql_update(modifications)
            for mod_type, triples in modifications.items():
                for triple in triples:
                    modification_text += f"<p><strong>{mod_type}</strong>: {filter.human_readable_predicate(triple[1], subject_classes)} {filter.human_readable_predicate(triple[2], subject_classes)}</p>"
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
    try:
        timestamp_dt = datetime.fromisoformat(timestamp)
    except ValueError:
        query_timestamp = f'''
            SELECT ?generation_time
            WHERE {{
                <{entity_uri}/prov/se/{timestamp}> <{ProvEntity.iri_generated_at_time}> ?generation_time.
            }}
        '''
        provenance_sparql.setQuery(query_timestamp)
        provenance_sparql.setReturnFormat(JSON)
        try:
            generation_time = provenance_sparql.queryAndConvert()['results']['bindings'][0]['generation_time']['value']
        except IndexError:
            abort(404)
        timestamp = generation_time
        timestamp_dt = datetime.fromisoformat(generation_time)
    agnostic_entity = AgnosticEntity(res=entity_uri, config_path=change_tracking_config)
    history, metadata, other_snapshots_metadata = agnostic_entity.get_state_at_time(time=(None, timestamp), include_prov_metadata=True)
    subject_classes = [subject_class[2] for subject_class in list(history.values())[0].triples((URIRef(entity_uri), RDF.type, None))]
    all_snapshots = list(metadata.items()) + list(other_snapshots_metadata.items())
    sorted_all_snapshots = sorted(all_snapshots, key=lambda x: x[1]['generatedAtTime'])
    last_snapshot_timestamp = sorted_all_snapshots[-1][1]['generatedAtTime'] if sorted_all_snapshots else None
    if last_snapshot_timestamp and timestamp > last_snapshot_timestamp:
        abort(404)
    if not timestamp_dt.tzinfo:
        timestamp_dt = timestamp_dt.replace(tzinfo=timezone.utc)
    history = {k: v for k, v in history.items()}
    for key, value in metadata.items():
        value['generatedAtTime'] = datetime.fromisoformat(value['generatedAtTime']).astimezone(timezone.utc).isoformat()
    try:
        closest_timestamp = min(history.keys(), key=lambda t: abs(datetime.fromisoformat(t).astimezone(timezone.utc) - timestamp_dt))
    except ValueError:
        abort(404)
    version: Graph = history[closest_timestamp]
    triples = list(version.triples((None, None, None)))
    relevant_properties = set()
    if display_rules:
        grouped_triples, relevant_properties = get_grouped_triples(entity_uri, triples, subject_classes)
    else:
        grouped_triples = dict()
        for triple in triples:
            grouped_triples.setdefault((triple[1], filter.human_readable_predicate(triple[1], subject_classes, True), False), []).append(triple)
        relevant_properties = set(triple[1] for triple in triples)

    if relevant_properties:
        triples = [triple for triple in triples if str(triple[1]) in relevant_properties]

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
    return render_template('entity_version.jinja', subject=entity_uri, metadata=closest_metadata, timestamp=closest_timestamp, next_snapshot_timestamp=next_snapshot_timestamp, prev_snapshot_timestamp=prev_snapshot_timestamp, modifications=modifications, grouped_triples=grouped_triples, subject_classes=subject_classes)

@app.route('/restore-version/<path:entity_uri>/<timestamp>', methods=['POST'])
def restore_version(entity_uri, timestamp):
    query_snapshots = f"""
        SELECT ?time ?updateQuery ?snapshot
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
    sum_update_queries = ""
    for relevant_result in relevant_results:
        for result in results:
            if result[1]:
                if convert_to_datetime(result[0]) > convert_to_datetime(relevant_result[0]):
                    sum_update_queries += (result[1]) +  ";"
        primary_source = URIRef(relevant_result[2])
    inverted_query = invert_sparql_update(sum_update_queries)
    editor = Editor(dataset_endpoint, provenance_endpoint, app.config['COUNTER_HANDLER'], app.config['RESPONSIBLE_AGENT'], primary_source, app.config['DATASET_GENERATION_TIME'])
    editor.execute(inverted_query)
    query = f"""
        SELECT ?predicate ?object WHERE {{
            <{entity_uri}> ?predicate ?object.
        }}
    """
    sparql.setQuery(query)
    sparql.setReturnFormat(JSON)
    return redirect(url_for('show_triples', subject=entity_uri))

@app.errorhandler(404)
def page_not_found(e):
    return render_template('errors/404.jinja'), 404

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

def validate_new_triple(subject, predicate, new_value, old_value = None):
    data_graph = fetch_data_graph_for_subject(subject)
    s_types = [triple[2] for triple in data_graph.triples((URIRef(subject), RDF.type, None))]
    if old_value is not None:
        old_value = [triple[2] for triple in data_graph.triples((URIRef(subject), URIRef(predicate), None)) if str(triple[2]) == str(old_value)][0]
    valid_value = Literal(new_value, datatype=old_value.datatype) if old_value.datatype and isinstance(old_value, Literal) else Literal(new_value) if isinstance(old_value, Literal) else URIRef(new_value)
    query = f"""
        PREFIX sh: <http://www.w3.org/ns/shacl#>
        SELECT DISTINCT ?property ?datatype ?a_class ?classIn (GROUP_CONCAT(DISTINCT COALESCE(?optionalValue, ""); separator=",") AS ?optionalValues)
        WHERE {{
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
            OPTIONAL {{
                ?property sh:in ?list .
                ?list rdf:rest*/rdf:first ?optionalValue .
            }}
        }}
        GROUP BY ?property ?datatype ?a_class ?classIn
    """
    results = shacl.query(query)
    property_exists = [row.property for row in results]
    if not property_exists:
        return None, old_value, gettext('The property %(predicate)s is not allowed for resources of type %(s_type)s', predicate=filter.human_readable_predicate(predicate, s_types), s_type=filter.human_readable_predicate(s_types[0], s_types))
    datatypes = [row.datatype for row in results]
    classes = [row.a_class for row in results if row.a_class]
    classes.extend([row.classIn for row in results if row.classIn])
    optional_values_str = [row.optionalValues for row in results if row.optionalValues]
    optional_values_str = optional_values_str[0] if optional_values_str else ''
    optional_values = [value for value in optional_values_str.split(',') if value]
    if optional_values and new_value not in optional_values:
        return None, old_value, gettext('<code>%(new_value)s</code> is not a valid value. The <code>%(property)s</code> property requires one of the following values: %(o_values)s', new_value=filter.human_readable_predicate(new_value, s_types), property=filter.human_readable_predicate(predicate, s_types), o_values=', '.join([f'<code>{filter.human_readable_predicate(value, s_types)}</code>' for value in optional_values]))
    if classes:
        if not validators.url(new_value):
            return None, old_value, gettext('<code>%(new_value)s</code> is not a valid value. The <code>%(property)s</code> property requires values of type %(o_types)s', new_value=filter.human_readable_predicate(new_value, s_types), property=filter.human_readable_predicate(predicate, s_types), o_types=', '.join([f'<code>{filter.human_readable_predicate(o_class, s_types)}</code>' for o_class in classes]))
        valid_value = convert_to_matching_class(new_value, classes)
        if valid_value is None:
            return None, old_value, gettext('<code>%(new_value)s</code> is not a valid value. The <code>%(property)s</code> property requires values of type %(o_types)s', new_value=filter.human_readable_predicate(new_value, s_types), property=filter.human_readable_predicate(predicate, s_types), o_types=', '.join([f'<code>{filter.human_readable_predicate(o_class, s_types)}</code>' for o_class in classes]))
        return valid_value, old_value, ''
    elif datatypes:
        valid_value = convert_to_matching_literal(new_value, datatypes)
        if valid_value is None:
            return None, old_value, gettext('<code>%(new_value)s</code> is not a valid value. The <code>%(property)s</code> property requires values of type %(o_types)s', new_value=filter.human_readable_predicate(new_value, s_types), property=filter.human_readable_predicate(predicate, s_types), o_types=', '.join([f'<code>{filter.human_readable_predicate(datatype, s_types)}</code>' for datatype in datatypes]))
        return valid_value, old_value, ''
    return valid_value, old_value, ''

def get_grouped_triples(subject, triples, subject_classes):
    grouped_triples = OrderedDict()
    relevant_properties = set()
    subject_classes = [str(subject_class) for subject_class in subject_classes]
    for rule in display_rules:
        if rule['class'] in subject_classes:
            for prop in rule['displayProperties']:
                relevant_properties.add(prop['property'])
                for displayConfig in prop['values']:
                    if displayConfig['shouldBeDisplayed']:
                        for triple in triples:
                            if str(triple[1]) == prop['property']:
                                display_name = displayConfig['displayName']
                                if display_name not in grouped_triples:
                                    grouped_triples[display_name] = {
                                        'property': prop['property'],
                                        'external_entity': None,
                                        'triples': []
                                    }
                                if displayConfig['fetchValueFromQuery']:
                                    result, external_entity = execute_sparql_query(displayConfig['fetchValueFromQuery'], subject, triple[2])
                                    if result:
                                        grouped_triples[display_name]['external_entity'] = external_entity
                                        new_triple = (str(triple[0]), str(triple[1]), str(result))
                                        existing_values = [t[2] for t in grouped_triples[display_name]['triples']]
                                        if new_triple[2] not in existing_values:
                                            grouped_triples[display_name]['triples'].append(new_triple)
                                else:
                                    new_triple = (str(triple[0]), str(triple[1]), str(triple[2]))
                                    existing_values = [t[2] for t in grouped_triples[display_name]['triples']]
                                    if new_triple[2] not in existing_values:
                                        grouped_triples[display_name]['triples'].append(new_triple)
    ordered_display_names = sorted(grouped_triples.keys(), key=lambda k: property_order_index(grouped_triples[k]['property'], subject_classes))
    grouped_triples = OrderedDict((k, grouped_triples[k]) for k in ordered_display_names)
    return grouped_triples, relevant_properties

def property_order_index(prop, subject_classes):
    """Return the index of a property based on its order in the configuration file."""
    for rule in display_rules:
        if rule['class'] in subject_classes:
            for index, prop_config in enumerate(rule['displayProperties']):
                if prop_config['property'] == prop:
                    return index
    return float('inf')

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
            return Literal(object_value, datatype=XSD.string)
        is_valid_datatype = validation_func(object_value)
        if is_valid_datatype:
            return Literal(object_value, datatype=datatype)

def invert_sparql_update(sparql_query: str) -> str:
    inverted_query = sparql_query.replace('INSERT', 'TEMP_REPLACE').replace('DELETE', 'INSERT').replace('TEMP_REPLACE', 'DELETE')
    return inverted_query

def execute_sparql_update(sparql_query: str):
    editor = Editor(dataset_endpoint, provenance_endpoint, app.config['COUNTER_HANDLER'], app.config['RESPONSIBLE_AGENT'], app.config['PRIMARY_SOURCE'], app.config['DATASET_GENERATION_TIME'])
    editor.execute(sparql_query)

def prioritize_datatype(datatypes):
    for datatype in DATATYPE_MAPPING:
        if datatype[0] in datatypes:
            return datatype[0]
    return DATATYPE_MAPPING[0][0]

def get_valid_predicates(triples):
    existing_predicates = [triple[1] for triple in triples]
    predicate_counts = {str(predicate): existing_predicates.count(predicate) for predicate in set(existing_predicates)}
    default_datatypes = {str(predicate): XSD.string for predicate in existing_predicates}
    if not shacl:
        return existing_predicates, existing_predicates, default_datatypes, dict(), dict()
    s_types = [triple[2] for triple in triples if triple[1] == RDF.type]
    if not s_types:
        return existing_predicates, existing_predicates, default_datatypes, dict(), dict()
    query = prepareQuery(f"""
        SELECT ?predicate ?datatype ?maxCount ?minCount ?hasValue (GROUP_CONCAT(?optionalValue; separator=",") AS ?optionalValues) WHERE {{
            ?shape sh:targetClass ?type ;
                   sh:property ?property .
            VALUES ?type {{<{'> <'.join(s_types)}>}}
            ?property sh:path ?predicate .
            OPTIONAL {{?property sh:datatype ?datatype .}}
            OPTIONAL {{?property sh:maxCount ?maxCount .}}
            OPTIONAL {{?property sh:minCount ?minCount .}}
            OPTIONAL {{?property sh:hasValue ?hasValue .}}
            OPTIONAL {{
                ?property sh:in ?list .
                ?list rdf:rest*/rdf:first ?optionalValue .
            }}
            OPTIONAL {{
                ?property sh:or ?orList .
                ?orList rdf:rest*/rdf:first ?orConstraint .
                ?orConstraint sh:datatype ?datatype .
            }}
            FILTER (isURI(?predicate))
        }}
        GROUP BY ?predicate ?datatype ?maxCount ?minCount ?hasValue
    """, initNs={"sh": "http://www.w3.org/ns/shacl#", "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#"})
    results = shacl.query(query)
    valid_predicates = [{
        str(row.predicate): {
            "min": (None if row.minCount is None else str(row.minCount)), 
            "max": (None if row.maxCount is None else str(row.maxCount)),
            "hasValue": row.hasValue,
            "optionalValues": row.optionalValues.split(",") if row.optionalValues else []
        }
    } for row in results]
    can_be_added = set()
    can_be_deleted = set()
    for valid_predicate in valid_predicates:
        for predicate, ranges in valid_predicate.items():
            if ranges["hasValue"]:
                mandatory_value_present = any(triple[2] == str(ranges["hasValue"]) for triple in triples)
                if not mandatory_value_present:
                    can_be_added.add(predicate)
            else:
                max_reached_for_generic = (ranges["max"] is not None and int(ranges["max"]) <= predicate_counts.get(predicate, 0))
                max_reached_for_optional = (ranges["max"] is not None and int(ranges["max"]) <= sum(1 for triple in triples if triple[2] in ranges["optionalValues"])) if ranges["optionalValues"] else max_reached_for_generic
                if not max_reached_for_generic or not max_reached_for_optional:
                    can_be_added.add(predicate)
                if not (ranges["min"] is not None and int(ranges["min"]) == predicate_counts.get(predicate, 0)):
                    can_be_deleted.add(predicate)
    datatypes = defaultdict(list)
    for row in results:
        if row.datatype:
            datatypes[str(row.predicate)].append(row.datatype)
        else:
            datatypes[str(row.predicate)].append(XSD.string)
    mandatory_values = {}
    for valid_predicate in valid_predicates:
        for predicate, ranges in valid_predicate.items():
            if ranges["hasValue"]:
                mandatory_values[str(predicate)] = str(ranges["hasValue"])
    optional_values = dict()
    for valid_predicate in valid_predicates:
        for predicate, ranges in valid_predicate.items():
            if "optionalValues" in ranges:
                optional_values.setdefault(str(predicate), list()).extend(ranges["optionalValues"])
    return list(can_be_added), list(can_be_deleted), dict(datatypes), mandatory_values, optional_values, s_types

def execute_sparql_query(query: str, subject: str, value: str) -> Tuple[str, str]:
    query = query.replace('[[subject]]', f'<{subject}>')
    query = query.replace('[[value]]', f'<{value}>')
    sparql.setQuery(query)
    sparql.setReturnFormat(JSON)
    results = sparql.query().convert().get("results", {}).get("bindings", [])
    if results:
        parsed_query = parseQuery(query)
        algebra_query = translateQuery(parsed_query).algebra
        variable_order = algebra_query['PV']
        result = results[0]
        values = [result.get(str(var_name), {}).get("value", None) for var_name in variable_order]
        first_value = values[0] if len(values) > 0 else None
        second_value = values[1] if len(values) > 1 else None
        return (first_value, second_value)
    return None, None

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