from rdflib import XSD

DATATYPE_MAPPING = [
    (XSD.string, 'text', dict()),
    (XSD.normalizedString, 'text', dict()),

    (XSD.integer, 'number', dict()),
    (XSD.int, 'number', dict()),
    (XSD.positiveInteger, 'number'),
    (XSD.negativeInteger, 'number', dict()),
    (XSD.nonNegativeInteger, 'number', dict()),
    (XSD.nonPositiveInteger, 'number', dict()),
    (XSD.byte, 'number', dict()),
    (XSD.short, 'number', dict()),
    (XSD.long, 'number', dict()),
    (XSD.unsignedByte, 'number', dict()),
    (XSD.unsignedShort, 'number', dict()),
    (XSD.unsignedLong, 'number', dict()),
    (XSD.unsignedInt, 'number', dict()),

    (XSD.float, 'number', {'step': 'any'}),
    (XSD.double, 'number', {'step': 'any'}),
    (XSD.decimal, 'number', {'step': 'any'}),

    (XSD.duration, 'text', dict()),
    (XSD.dayTimeDuration, 'text', dict()),
    (XSD.yearMonthDuration, 'text', dict()),

    (XSD.dateTime, 'datetime-local', dict()),
    (XSD.dateTimeStamp, 'datetime-local', dict()),

    (XSD.date, 'date', dict()),

    (XSD.gYear, 'year', dict()),

    (XSD.gYearMonth, 'month', dict()),

    (XSD.time, 'time', dict()),
    (XSD.hour, 'time', dict()),
    (XSD.timezoneOffset, 'time', dict()),
    (XSD.minute, 'time', dict()),
    (XSD.second, 'time', dict()),

    (XSD.boolean, 'checkbox', dict()),

    (XSD.hexBinary, 'password', dict()),
    (XSD.base64Binary, 'password', dict()),

    (XSD.anyURI, 'url', dict()),
    
    (XSD.QName, 'text', dict()),
    (XSD.ENTITIES, 'text', dict()),
    (XSD.ENTITY, 'text', dict()),
    (XSD.ID, 'text', dict()),
    (XSD.IDREF, 'text', dict()),
    (XSD.IDREFS, 'text', dict()),
    (XSD.NCName, 'text', dict()),
    (XSD.NMTOKEN, 'text', dict()),
    (XSD.NMTOKENS, 'text', dict()),
    (XSD.NOTATION, 'text', dict()),
    (XSD.Name, 'text', dict())
]