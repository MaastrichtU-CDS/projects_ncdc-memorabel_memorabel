import csv
import pandas as pd
from postgres_manager import PostgresManager
from cdm_builder import *
from constants import *
from utils import arrays_to_dict, parse_date, get_year_of_birth

CDM_SQL = {
    CONDITION_OCCURRENCE: build_condition,
    MEASUREMENT: build_measurement,
    OBSERVATION: build_observation
}

class DataParser:
    """ Parses the dataset to the OMOP CDM.
    """
    def __init__(self, path, source_mapping, destination_mapping, cohort_id, pg):
        self.path = path
        self.source_mapping = source_mapping
        self.destination_mapping = destination_mapping
        self.cohort_id = cohort_id
        self.pg = pg
        self.warnings = []

        # Retrieve the necessary information from the mappings
        self.value_mapping = self.create_value_mapping()
        (self.date_source_variable, self.date_format) = self.get_parameters(DATE, with_format=True)

    @staticmethod
    def variable_values_to_dict(keys, values, separator=DEFAULT_SEPARATOR):
        """ Convert the string representing the variable values map to a dictionary.
        """
        return arrays_to_dict(
            keys.split(separator),
            values.split(separator)
        )
    
    @staticmethod
    def valid_row_value(variable, row):
        """ Validate if the value exists and is not null
        """
        # TODO: Handling missing values in another way
        return variable in row and row[variable] and not pd.isnull(row[variable])

    def map_variable_values(self, variable, specification):
        """ Create the mapping between a source and destination variable
        """
        mapping = {}
        source_values = self.variable_values_to_dict(
            specification[VALUES],
            specification[VALUES_PARSED]
        )
        if self.destination_mapping[variable][VALUES_CONCEPT_ID]:
            # Mapping each value to the concept ID defined
            destination_values = self.variable_values_to_dict(
                self.destination_mapping[variable][VALUES],
                self.destination_mapping[variable][VALUES_CONCEPT_ID]
            )
            mapping = {map_key: destination_values[map_value] \
                for map_key, map_value in source_values.items()}
            mapping[VALUE_AS_CONCEPT_ID] = True
        else:
            # Mapping each value to another string
            mapping = source_values
            mapping[VALUE_AS_CONCEPT_ID] = False
        return mapping
    
    def create_value_mapping(self):
        """ Create the mapping between the source and destination values for each variable
        """
        value_mapping = {}
        for key, value in self.source_mapping.items():
            if value[VALUES]:
                if not value[VALUES_PARSED]:
                    raise Exception(f'Error in the source mapping for variable {key}')
                if key not in self.destination_mapping or VALUES_CONCEPT_ID not in self.destination_mapping[key]:
                    raise Exception(f'Variable {key} is not correctly mapped in the destination mapping!')
                value_mapping[key] = self.map_variable_values(key, value)
        return value_mapping

    def parse_dataset(self, start, limit):
        """ Parse the dataset to the CDM format.
        """
        print(f'Parse dataset from file {self.path}')

        kwargs = {
            'start': start,
            'limit': limit,
        } 

        reader = None
        if '.csv' in self.path:
            with open(self.path) as csv_file:
                csv_reader = csv.DictReader(csv_file, delimiter=',')
                for i in range(start):
                    next(csv_reader)
                self.transform_rows(enumerate(csv_reader, start=start), **kwargs)
        elif '.sav' in self.path:
            df = pd.read_spss(self.path)
            self.transform_rows(df.loc[start:].iterrows(), **kwargs)
        elif '.sas' in self.path:
            df = pd.read_sas(self.path)
            self.transform_rows(df.loc[start:].iterrows(), **kwargs)

    def get_parameters(self, parameter, with_format=False):
        """ Returns the source variable and format for a parameter.
        """
        parameter_source_variable = None
        parameter_format = None
        if parameter and parameter in self.source_mapping:
            parameter_source_variable = self.source_mapping[parameter][SOURCE_VARIABLE]
            if with_format:
                parameter_format = self.source_mapping[parameter][FORMAT]
        return (parameter_source_variable, parameter_format)

    def get_parsed_value(self, variable, value):
        """ Get the parsed value for a variable.
        """
        if variable in self.value_mapping:
            if str(value) in self.value_mapping[variable]:
                return (self.value_mapping[variable][VALUE_AS_CONCEPT_ID], self.value_mapping[variable][str(value)])
            elif DEFAULT_VALUE in self.value_mapping[variable]:
                return (self.value_mapping[variable][VALUE_AS_CONCEPT_ID], self.value_mapping[variable][DEFAULT_VALUE])
            raise Exception(f'Variable {variable} is incorrectly mapped: value {value} is not mapped')
        return (False, value)

    def get_source_variable(self, variable):
        """ Check if there is a map for the source id.
        """
        return self.source_mapping[variable][SOURCE_VARIABLE] if variable in self.source_mapping else None

    def parse_person(self, row):
        """ Parse the person information from the row.
        """
        sex_source_variable = self.get_source_variable(GENDER)
        birth_year_source_variable = self.get_source_variable(YEAR_OF_BIRTH)
        birth_year = None
        if birth_year_source_variable and self.valid_row_value(birth_year_source_variable, row):
            birth_year = row[birth_year_source_variable]
        else:
            # The year of birth is required to create an entry for the person. In case that 
            # variable isn't provided, the year of birth will be obtained from a variable indicating
            # the age for a particular date.
            # TODO: Use alternatives to have all the other variables instead of prefix
            age_variables = [ k for k in self.destination_mapping.keys() if AGE_PREFIX in k ]
            if len(age_variables) > 0:
                for age_variable in age_variables:
                    age_source_variable = self.get_source_variable(age_variable)
                    (age_date_variable, age_date_format) = self.get_parameters(
                        self.destination_mapping[age_variable][DATE], with_format=True)
                    if all([var and self.valid_row_value(var, row) for var in [age_source_variable, age_date_variable]]):
                        try:
                            birth_year = get_year_of_birth(int(row[age_source_variable]), \
                                row[age_date_variable], age_date_format or self.date_format)
                            break
                        except Exception as error:
                            print(f'Error parsing year of birth from variable {age_variable}')
                            print(str(error))
        if not birth_year:
            raise Exception('Missing required information, the row should contain the year of birth.')

        # Handling death information
        death_datetime = None
        death_time_source_variable = self.get_source_variable(DEATH_DATE)
        death_flag_source_variable = self.get_source_variable(DEATH_FLAG)
        if death_time_source_variable and self.valid_row_value(death_time_source_variable, row):
            death_datetime = parse_date(row[death_time_source_variable], self.date_format, DATE_FORMAT)
        elif death_flag_source_variable and self.valid_row_value(death_flag_source_variable, row):
            (value_as_concept, parsed_value) = self.get_parsed_value(DEATH_FLAG, row[death_flag_source_variable])
            if parsed_value and parsed_value == 'True':
                death_datetime = DATE_DEFAULT

        # Add a new entry for the person/patient
        person_sql = build_person(
            self.get_parsed_value(GENDER, row[sex_source_variable])[1],
            birth_year,
            self.cohort_id,
            death_datetime,
        )
        person_id = self.pg.run_sql(*person_sql, fetch_one=True)

        return person_id

    def transform_rows(self, iterator, start, limit):
        """ Transform each row in the dataset
        """
        id_map = {}
        processed_records = 0
        skipped_records = 0
        id_source_variable = self.get_source_variable(SOURCE_ID)
        for index, row in iterator:
            if limit > 0 and index - start >= limit:
                break
            try:
                # Check if the source id variable is provided. In that case,
                # the link between the source id and the person id will be stored
                # in a dictionary and in a temporary table.
                person_id = None
                if id_source_variable:
                    if not self.valid_row_value(id_source_variable, row):
                        raise Exception('Error when parsing the source id.')
                    source_id = row[id_source_variable]
                    if source_id in id_map:
                        person_id = id_map[source_id]
                    else:
                        # First check if it's already included in the temporary table.
                        person_id = get_person_id(source_id, self.cohort_id, self.pg)
                        if not person_id:
                            person_id = self.parse_person(row)
                            insert_id_record(source_id, person_id, self.cohort_id, self.pg)
                        id_map[source_id] = person_id
                else:
                    person_id = self.parse_person(row)
                #Parse the row
                self.transform_row(row, person_id)
                processed_records += 1
            except Exception as error:
                # TODO: Use a logger and add this information in a file
                print(f'Skipped record {index} due to an error:', str(error))
                skipped_records += 1
        print(f'Processed {processed_records} records and skipped {skipped_records} records due to errors')

    def transform_row(self, row, person_id):
        """ Transform each row and insert in the database.
        """
        # Parse the date for the observation/measurement/condition if available
        # TODO: Calculating the end data when provided with a period for the wave
        visit_id = None
        if self.date_source_variable in row:
           visit_date = parse_date(row[self.date_source_variable], self.date_format, DATE_FORMAT)
           visit_id = insert_visit_occurrence(person_id, visit_date, visit_date, self.pg)

        # Parse the observations/measurements/conditions
        for key, value in self.source_mapping.items():
            if key not in self.destination_mapping:
                if DATE not in key.lower() and key not in self.warnings:
                    print(f'Skipped variable {key} since its not mapped')
                    self.warnings.append(key)
            elif self.destination_mapping[key][DOMAIN] not in CDM_SQL:
                if self.destination_mapping[key][DOMAIN] not in [PERSON, NOT_APPLICABLE] and \
                    key not in self.warnings:
                    print(f'Skipped variable {key} since its domain is not currently accepted')
                    self.warnings.append(key)
            else:
                source_value = None
                if value[SOURCE_VARIABLE]:
                    source_variables = [value[SOURCE_VARIABLE]]
                    if value[ALTERNATIVES]:
                        source_variables.extend(value[ALTERNATIVES].split(DEFAULT_SEPARATOR))
                    # Check the first variable for the field that it's valid
                    for source_variable in source_variables:
                        if self.valid_row_value(source_variable, row):
                            source_value = row[source_variable]
                            if value[CONDITION] and row[source_variable] in value[CONDITION].split(DEFAULT_SEPARATOR):
                                break
                elif value[STATIC_VALUE]:
                    source_value = value[STATIC_VALUE]

                if source_value:
                    # TODO: improve the mapping between a variable and multiple
                    # source variables
                    domain = self.destination_mapping[key][DOMAIN]
                    (value_as_concept, parsed_value) = self.get_parsed_value(key, source_value)
                    if parsed_value != '_':
                        # Check if there is a specific date for the variable
                        date = DATE_DEFAULT
                        (source_date, source_date_format) = self.get_parameters(self.destination_mapping[key][DATE], with_format=True)
                        if source_date and source_date in row[source_date]:
                            try:
                                date = parse_date(row[source_date], source_date_format or self.date_format, DATE_FORMAT)
                            except Exception as error:
                                print(f'Error parsing a malformated date for variable {key}:')
                                print(error)
                        # Create the necessary arguments to build the SQL statement
                        named_args = {
                            'source_value': source_value,
                            'date': date,
                            'visit_id': visit_id
                        }
                        if value_as_concept:
                            named_args['value_as_concept'] = parsed_value
                        else:
                            named_args['value'] = parsed_value
                        # Check if there is a field for additional information
                        additional_info = self.destination_mapping[key][ADDITIONAL_INFO]
                        if additional_info and additional_info in self.source_mapping:
                            additional_info_value = self.source_mapping[additional_info][STATIC_VALUE]
                            if additional_info_value:
                                named_args['additional_info'] = additional_info_value
                            else:
                                additional_info_varible = self.source_mapping[additional_info][SOURCE_VARIABLE]
                                if additional_info_varible and self.valid_row_value(additional_info_varible, row):
                                    named_args['additional_info'] = f'{additional_info_varible}: \
                                        {self.get_parsed_value(additional_info_varible, row[additional_info_varible])[1]}'
                        elif value[STATIC_VALUE]:
                            named_args['additional_info'] = value[STATIC_VALUE]

                        # Run the SQL script to insert the measurement/observation/condition
                        self.pg.run_sql(*CDM_SQL[domain](person_id, self.destination_mapping[key], **named_args))