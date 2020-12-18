import re
from math import floor, ceil
from decimal import Decimal

from django.db import transaction
from django.db.models import Max, F
from django.db.models.fields.related import ManyToManyField
from django.conf import settings

from baserow.core.exceptions import UserNotInGroupError

from .exceptions import RowDoesNotExist


class RowHandler:
    def prepare_values(self, fields, values):
        """
        Prepares a set of values so that they can be created or updated in the database.
        It will check if the value can actually be set and prepares the value based on
        the field type.

        :param fields: The returned fields object from the get_model method.
        :type fields: dict
        :param values: The values that need to be prepared with the field id or the
            string 'field_{id}' as key.
        :type values: dict
        :return: The prepared values with the field name as key.
        :rtype: dict
        """

        return {
            field['name']: field['type'].prepare_value_for_db(
                field['field'],
                values[field_id] if field_id in values else values[field['name']]
            )
            for field_id, field in fields.items()
            if field_id in values or field['name'] in values
        }

    def extract_field_ids_from_dict(self, values):
        """
        Extracts the field ids from a dict containing the values that need to
        updated. For example keys like 'field_2', '3', 4 will be seen ass field ids.

        :param values: The values where to extract the fields ids from.
        :type values: dict
        :return: A list containing the field ids as integers.
        :rtype: list
        """

        field_pattern = re.compile("^field_([0-9]+)$")
        # @TODO improve this function
        return [
            int(re.sub("[^0-9]", "", str(key)))
            for key in values.keys()
            if str(key).isnumeric() or field_pattern.match(str(key))
        ]

    def extract_manytomany_values(self, values, model):
        """
        Extracts the ManyToMany values out of the values because they need to be
        created and updated in a different way compared to a regular value.

        :param values: The values where to extract the manytomany values from.
        :type values: dict
        :param model: The model containing the fields. They key, which is also the
            field name, is used to check in the model if the value is a ManyToMany
            value.
        :type model: Model
        :return: The values without the manytomany values and the manytomany values
            in another dict.
        :rtype: dict, dict
        """

        manytomany_values = {}

        for name, value in values.items():
            model_field = model._meta.get_field(name)
            if isinstance(model_field, ManyToManyField):
                manytomany_values[name] = values[name]

        for name in manytomany_values.keys():
            del values[name]

        return values, manytomany_values

    def get_row(self, user, table, row_id, model=None):
        """
        Fetches a single row from the provided table.

        :param user: The user of whose behalf the row is requested.
        :type user: User
        :param table: The table where the row must be fetched from.
        :type table: Table
        :param row_id: The id of the row that must be fetched.
        :type row_id: int
        :param model: If the correct model has already been generated it can be
            provided so that it does not have to be generated for a second time.
        :type model: Model
        :raises UserNotInGroupError: When the user does not belong to the related group.
        :raises RowDoesNotExist: When the row with the provided id does not exist.
        :return: The requested row instance.
        :rtype: Model
        """

        if not model:
            model = table.get_model()

        group = table.database.group
        if not group.has_user(user):
            raise UserNotInGroupError(user, group)

        try:
            row = model.objects.get(id=row_id)
        except model.DoesNotExist:
            raise RowDoesNotExist(f'The row with id {row_id} does not exist.')

        return row

    def create_row(self, user, table, values=None, model=None, before=None):
        """
        Creates a new row for a given table with the provided values.

        :param user: The user of whose behalf the row is created.
        :type user: User
        :param table: The table for which to create a row for.
        :type table: Table
        :param values: The values that must be set upon creating the row. The keys must
            be the field ids.
        :type values: dict
        :param model: If a model is already generated it can be provided here to avoid
            having to generate the model again.
        :type model: Model
        :param before: If provided the new row will be placed right before that row
            instance.
        :type before: Table
        :raises UserNotInGroupError: When the user does not belong to the related group.
        :return: The created row instance.
        :rtype: Model
        """

        if not values:
            values = {}

        group = table.database.group
        if not group.has_user(user):
            raise UserNotInGroupError(user, group)

        if not model:
            model = table.get_model()

        values = self.prepare_values(model._field_objects, values)
        values, manytomany_values = self.extract_manytomany_values(values, model)

        if before:
            # Here we calculate the order value, which indicates the position of the
            # row, by subtracting a fraction of the row that it must be placed
            # before. The same fraction is also going to be subtracted from the other
            # rows that have been placed before. By using these fractions we don't
            # have to re-order every row in the table.
            change = Decimal('0.00000000000000000001')
            values['order'] = before.order - change
            model.objects.filter(
                order__gt=floor(values['order']),
                order__lte=values['order']
            ).update(order=F('order') - change)
        else:
            # Because the row is by default added as last, we have to figure out what
            # the highest order is and increase that by one. Because the order of new
            # rows should always be a whole number we round it up.
            values['order'] = ceil(
                model.objects.aggregate(max=Max('order')).get('max') or Decimal('0')
            ) + 1

        instance = model.objects.create(**values)

        for name, value in manytomany_values.items():
            getattr(instance, name).set(value)

        return instance

    def update_row(self, user, table, row_id, values, model=None):
        """
        Updates one or more values of the provided row_id.

        :param user: The user of whose behalf the change is made.
        :type user: User
        :param table: The table for which the row must be updated.
        :type table: Table
        :param row_id: The id of the row that must be updated.
        :type row_id: int
        :param values: The values that must be updated. The keys must be the field ids.
        :type values: dict
        :param model: If the correct model has already been generated it can be
            provided so that it does not have to be generated for a second time.
        :type model: Model
        :raises UserNotInGroupError: When the user does not belong to the related group.
        :raises RowDoesNotExist: When the row with the provided id does not exist.
        :return: The updated row instance.
        :rtype: Model
        """

        group = table.database.group
        if not group.has_user(user):
            raise UserNotInGroupError(user, group)

        if not model:
            field_ids = self.extract_field_ids_from_dict(values)
            model = table.get_model(field_ids=field_ids)

        # Because it is possible to have a different database for the user tables we
        # need to start another transaction here, otherwise it is not possible to use
        # the select_for_update function.
        with transaction.atomic(settings.USER_TABLE_DATABASE):
            try:
                row = model.objects.select_for_update().get(id=row_id)
            except model.DoesNotExist:
                raise RowDoesNotExist(f'The row with id {row_id} does not exist.')

            values = self.prepare_values(model._field_objects, values)
            values, manytomany_values = self.extract_manytomany_values(values, model)

            for name, value in values.items():
                setattr(row, name, value)

            row.save()

            for name, value in manytomany_values.items():
                getattr(row, name).set(value)

        return row

    def delete_row(self, user, table, row_id):
        """
        Deletes an existing row of the given table and with row_id.

        :param user: The user of whose behalf the change is made.
        :type user: User
        :param table: The table for which the row must be deleted.
        :type table: Table
        :param row_id: The id of the row that must be deleted.
        :type row_id: int
        :raises UserNotInGroupError: When the user does not belong to the related group.
        :raises RowDoesNotExist: When the row with the provided id does not exist.
        """

        group = table.database.group
        if not group.has_user(user):
            raise UserNotInGroupError(user, group)

        model = table.get_model(field_ids=[])

        try:
            row = model.objects.get(id=row_id)
        except model.DoesNotExist:
            raise RowDoesNotExist(f'The row with id {row_id} does not exist.')

        row.delete()
