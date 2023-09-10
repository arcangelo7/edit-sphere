import validators
from rdflib import ConjunctiveGraph, Literal, URIRef
from rdflib_ocdm.counter_handler.counter_handler import CounterHandler
from rdflib_ocdm.ocdm_graph import OCDMGraph
from rdflib_ocdm.storer import Storer, Reader
from SPARQLWrapper import POST, XML, SPARQLWrapper


class Editor:
    def __init__(self, dataset_endpoint: str, provenance_endpoint:str, counter_handler: CounterHandler, resp_agent: URIRef, source: URIRef = None):
        self.dataset_endpoint = dataset_endpoint
        self.provenance_endpoint = provenance_endpoint
        self.counter_handler = counter_handler
        self.resp_agent = resp_agent
        self.source = source

    def update(self, subject: str, predicate: str, old_value: str, new_value: str) -> None:
        subject = URIRef(subject)
        predicate = URIRef(predicate)
        g_set = OCDMGraph(self.counter_handler)
        Reader.import_entities_from_triplestore(g_set, self.dataset_endpoint, [subject])
        g_set.preexisting_finished(self.resp_agent, self.source)
        for triple in g_set.triples((None, predicate, None)):
            if str(triple[2]) == old_value:
                datatype = triple[2].datatype if isinstance(triple[2], Literal) else None
                g_set.remove(triple)
        new_value = URIRef(new_value) if validators.url(new_value) else Literal(new_value, datatype=datatype)
        g_set.add((subject, predicate, new_value))
        self.save(g_set)

    def delete(self, subject: str, predicate: str = None, value: str = None) -> None:
        subject = URIRef(subject)
        predicate = URIRef(predicate)
        g_set = OCDMGraph(self.counter_handler)
        Reader.import_entities_from_triplestore(g_set, self.dataset_endpoint, [subject])
        g_set.preexisting_finished(self.resp_agent, self.source)
        if predicate:
            if value:
                for triple in g_set.triples((None, predicate, None)):
                    if str(value) == str(triple[2]):
                        g_set.remove(triple)
        if len(g_set) == 0:
            g_set.mark_as_deleted(subject)
        self.save(g_set)

    def import_entity_from_triplestore(self, g_set: OCDMGraph, res_list: list):
        sparql: SPARQLWrapper = SPARQLWrapper(self.dataset_endpoint)
        query: str = f'''
            CONSTRUCT {{?s ?p ?o}} 
            WHERE {{
                ?s ?p ?o. 
                VALUES ?s {{<{'> <'.join(res_list)}>}}
        }}'''
        sparql.setQuery(query)
        sparql.setMethod(POST)
        sparql.setReturnFormat(XML)
        result: ConjunctiveGraph = sparql.queryAndConvert()
        if result is not None:
            for triple in result.triples((None, None, None)):
                g_set.add(triple)
        
    def save(self, g_set: OCDMGraph):
        g_set.generate_provenance()
        dataset_storer = Storer(g_set)
        prov_storer = Storer(g_set.provenance)
        dataset_storer.upload_all(self.dataset_endpoint)
        prov_storer.upload_all(self.provenance_endpoint)
        g_set.commit_changes()