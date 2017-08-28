"""
Filters for the task application
"""

from django import forms
from django.contrib.auth.models import User

from autocomplete_light import shortcuts as autocomplete_light
import django_filters

from muckrock.agency.models import Agency
from muckrock.filters import BLANK_STATUS, BOOLEAN_CHOICES, RangeWidget
from muckrock.jurisdiction.models import Jurisdiction
from muckrock.task.models import (
    SNAIL_MAIL_CATEGORIES,
    Task,
    ResponseTask,
    NewAgencyTask,
    SnailMailTask,
    FlaggedTask,
    StaleAgencyTask,
    RejectedEmailTask,
    FailedFaxTask,
)

class TaskFilterSet(django_filters.FilterSet):
    """Allows tasks to be filtered by whether they're resolved, and by who resolved them."""
    resolved = django_filters.BooleanFilter(
        label='Show Resolved',
        widget=forms.CheckboxInput())
    resolved_by = django_filters.ModelMultipleChoiceFilter(
        queryset=User.objects.all(),
        widget=autocomplete_light.MultipleChoiceWidget('UserTaskAutocomplete')
    )
    date_created = django_filters.DateFromToRangeFilter(
        label='Date Range',
        lookup_expr='contains',
        widget=RangeWidget(attrs={
            'class': 'datepicker',
            'placeholder': 'MM/DD/YYYY',
        }),
    )

    class Meta:
        model = Task
        fields = ['resolved', 'resolved_by']


class ResponseTaskFilterSet(TaskFilterSet):
    """Allows response tasks to be filtered by predicted status."""
    predicted_status = django_filters.ChoiceFilter(choices=BLANK_STATUS)
    class Meta:
        model = ResponseTask
        fields = ['predicted_status', 'resolved', 'resolved_by']


class NewAgencyTaskFilterSet(TaskFilterSet):
    """Allows new agency tasks to be filtered by jurisdiction."""
    class Meta:
        model = NewAgencyTask
        fields = ['agency__jurisdiction__level', 'resolved', 'resolved_by']


class SnailMailTaskFilterSet(TaskFilterSet):
    """Allows snail mail tasks to be filtered by category, as well as the
    presence of a tracking number or an agency note."""
    category = django_filters.ChoiceFilter(choices=[('', 'All')] + SNAIL_MAIL_CATEGORIES)
    has_tracking_number = django_filters.ChoiceFilter(
        method='filter_has_tracking_number',
        label='Has tracking number',
        choices=BOOLEAN_CHOICES,
    )
    has_agency_notes = django_filters.ChoiceFilter(
        method='filter_has_agency_notes',
        label='Has agency notes',
        choices=BOOLEAN_CHOICES,
    )
    resolved = django_filters.BooleanFilter(
        label='Show Resolved',
        widget=forms.CheckboxInput())
    resolved_by = django_filters.ModelMultipleChoiceFilter(
        queryset=User.objects.all(),
        widget=autocomplete_light.MultipleChoiceWidget('UserTaskAutocomplete')
    )

    def blank_choice(self, queryset, name, value):
        """Check if the value is blank"""
        # pylint: disable=no-self-use
        if value == 'True':
            return queryset.exclude(**{name: ''})
        elif value == 'False':
            return queryset.filter(**{name: ''})
        return queryset

    def filter_has_tracking_number(self, queryset, name, value):
        """Check if the foia has a tracking number."""
        #pylint: disable=unused-argument
        return self.blank_choice(queryset, 'communication__foia__tracking_id', value)

    def filter_has_agency_notes(self, queryset, name, value):
        """Check if the agency has notes."""
        #pylint: disable=unused-argument
        return self.blank_choice(queryset, 'communication__foia__agency__notes', value)

    class Meta:
        model = SnailMailTask
        fields = [
            'category',
            'has_tracking_number',
            'has_agency_notes',
            'resolved',
            'resolved_by',
        ]


class FlaggedTaskFilterSet(TaskFilterSet):
    """Allows a flagged task to be filtered by a user."""
    user = django_filters.ModelMultipleChoiceFilter(
        queryset=User.objects.all(),
        widget=autocomplete_light.MultipleChoiceWidget('UserAutocomplete')
    )

    class Meta:
        model = FlaggedTask
        fields = ['user', 'resolved', 'resolved_by']


class StaleAgencyTaskFilterSet(TaskFilterSet):
    """Allows a stale agency task to be filtered by jurisdiction."""
    jurisdiction = django_filters.ModelMultipleChoiceFilter(
        name='agency__jurisdiction',
        queryset=Jurisdiction.objects.filter(hidden=False),
        widget=autocomplete_light.MultipleChoiceWidget('JurisdictionAutocomplete')
    )
    class Meta:
        model = StaleAgencyTask
        fields = ['jurisdiction', 'resolved', 'resolved_by']


class RejectedEmailTaskFilterSet(TaskFilterSet):
    """Allows a rejected email task to be filtered by the to: email"""
    class Meta:
        model = RejectedEmailTask
        fields = ['email', 'resolved', 'resolved_by']


class FailedFaxTaskFilterSet(TaskFilterSet):
    """Allows a failed fax task to be filtered by the agency."""
    agency = django_filters.ModelMultipleChoiceFilter(
        name='communication__foia__agency',
        queryset=Agency.objects.get_approved(),
        widget=autocomplete_light.MultipleChoiceWidget('AgencyAutocomplete')
    )
    class Meta:
        model = FailedFaxTask
        fields = ['agency', 'resolved', 'resolved_by']
