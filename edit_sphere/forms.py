from wtforms import Form, HiddenField, SelectField, StringField
from wtforms.validators import DataRequired

from resources.datatypes import DATATYPE_MAPPING


class UpdateTripleForm(Form):
    subject = HiddenField('Subject')
    predicate = HiddenField('Predicate')
    old_value = HiddenField('Old Value')
    new_value = StringField('New Value', [DataRequired()])

class CreateTripleFormWithInput(Form):
    subject = HiddenField('Subject')
    predicate = StringField('Property', [DataRequired()])
    object = StringField('Value', [DataRequired()])

class CreateTripleFormWithSelect(Form):
    def __init__(self, *args, **kwargs):
        super(CreateTripleFormWithSelect, self).__init__(*args, **kwargs)
        predicate_choice = self.predicate.data
        datatype = next((predicate[1] for predicate in DATATYPE_MAPPING if str(predicate[0]) == predicate_choice), 'text')
        self.object.widget.input_type = datatype
    subject = HiddenField('Subject')
    predicate = SelectField('Property', choices=[], validators=[DataRequired()])
    object = StringField('Value', [DataRequired()])