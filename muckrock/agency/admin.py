"""
Admin registration for Agency models
"""

from django import forms
from django.conf.urls import url
from django.contrib import admin, messages
from django.contrib.auth.models import User
from django.core.validators import validate_email
from django.shortcuts import render, redirect
from django.template.defaultfilters import slugify

from adaptor.model import CsvModel
from adaptor.fields import CharField, DjangoModelField
from reversion.admin import VersionAdmin
from autocomplete_light import shortcuts as autocomplete_light
import logging
import sys

from muckrock.agency.models import (
        AgencyType,
        Agency,
        AgencyAddress,
        AgencyEmail,
        AgencyPhone,
        )
from muckrock.agency.forms import CSVImportForm
from muckrock.communication.models import (
        Address,
        EmailAddress,
        PhoneNumber,
        )
from muckrock.jurisdiction.models import Jurisdiction

logger = logging.getLogger(__name__)

# These inhereit more than the allowed number of public methods
# pylint: disable=too-many-public-methods

class AgencyTypeAdmin(VersionAdmin):
    """AgencyType admin options"""
    list_display = ('name', )
    search_fields = ['name']


class AgencyAddressAdminForm(forms.ModelForm):
    """AgencyAddress Inline admin form"""
    address = autocomplete_light.ModelChoiceField(
            'AddressAutocomplete',
            queryset=Address.objects.all(),
            )

    class Meta:
        model = AgencyAddress
        fields = '__all__'


class AgencyAddressInline(admin.TabularInline):
    """Inline for agency's addresses"""
    model = AgencyAddress
    form = AgencyAddressAdminForm
    extra = 1


class AgencyEmailAdminForm(forms.ModelForm):
    """AgencyEmail Inline admin form"""
    email = autocomplete_light.ModelChoiceField(
            'EmailAddressAutocomplete',
            queryset=EmailAddress.objects.all(),
            )

    class Meta:
        model = AgencyEmail
        fields = '__all__'


class AgencyEmailInline(admin.TabularInline):
    """Inline for agency's email addresses"""
    model = AgencyEmail
    form = AgencyEmailAdminForm
    extra = 1


class AgencyPhoneAdminForm(forms.ModelForm):
    """AgencyPhone Inline admin form"""
    phone = autocomplete_light.ModelChoiceField(
            'PhoneNumberAutocomplete',
            queryset=PhoneNumber.objects.all(),
            )

    class Meta:
        model = AgencyPhone
        fields = '__all__'


class AgencyPhoneInline(admin.TabularInline):
    """Inline for agency's phone numbers"""
    model = AgencyPhone
    form = AgencyPhoneAdminForm
    extra = 1


class AgencyAdminForm(forms.ModelForm):
    """Agency admin form to order users"""
    user = autocomplete_light.ModelChoiceField(
            'UserAutocomplete',
            queryset=User.objects.all(),
            required=False)
    jurisdiction = autocomplete_light.ModelChoiceField(
            'JurisdictionAdminAutocomplete',
            queryset=Jurisdiction.objects.all())
    appeal_agency = autocomplete_light.ModelChoiceField(
            'AgencyAppealAdminAutocomplete',
            queryset=Agency.objects.all(),
            required=False)
    payable_to = autocomplete_light.ModelChoiceField(
            'AgencyAdminAutocomplete',
            queryset=Agency.objects.all(),
            required=False)
    parent = autocomplete_light.ModelChoiceField(
            'AgencyAdminAutocomplete',
            queryset=Agency.objects.all(),
            required=False)

    class Meta:
        # pylint: disable=too-few-public-methods
        model = Agency
        fields = '__all__'


class AgencyAdmin(VersionAdmin):
    """Agency admin options"""
    change_list_template = 'admin/agency/agency/change_list.html'
    prepopulated_fields = {'slug': ('name',)}
    list_display = ('name', 'jurisdiction', 'status')
    list_filter = ['status', 'types']
    search_fields = ['name']
    filter_horizontal = ('types',)
    form = AgencyAdminForm
    formats = ['xls', 'csv']
    inlines = (
            AgencyAddressInline,
            AgencyEmailInline,
            AgencyPhoneInline,
            )
    # deprecated fields are set to read only
    readonly_fields = (
            'can_email_appeals',
            'address',
            'email',
            'other_emails',
            'phone',
            'fax',
            )
    fieldsets = (
            (None, {
                'fields': (
                    'name',
                    'slug',
                    'jurisdiction',
                    'types',
                    'status',
                    'user',
                    'appeal_agency',
                    'payable_to',
                    'image',
                    'image_attr_line',
                    'public_notes',
                    'stale',
                    'manual_stale',
                    'location',
                    'contact_salutation',
                    'contact_first_name',
                    'contact_last_name',
                    'contact_title',
                    'url',
                    'notes',
                    'aliases',
                    'parent',
                    'website',
                    'twitter',
                    'twitter_handles',
                    'foia_logs',
                    'foia_guide',
                    'exempt',
                    'requires_proxy',
                    ),
                }),
            ('Deprecated', {
                'classes': ('collapse',),
                'fields': (
                    'can_email_appeals',
                    'address',
                    'email',
                    'other_emails',
                    'phone',
                    'fax',
                    ),
                'description': 'These values are no longer actively used.  '
                'They are here to view on old data only.  If you find yourself '
                'needing to look here often, something is probably wrong and '
                'you should file a bug',
                }))

    def get_urls(self):
        """Add custom URLs here"""
        urls = super(AgencyAdmin, self).get_urls()
        my_urls = [url(
            r'^import/$',
            self.admin_site.admin_view(self.csv_import),
            name='agency-admin-import',
            )]
        return my_urls + urls

    def csv_import(self, request):
        """Import a CSV file of agencies"""
        # pylint: disable=no-self-use
        # pylint: disable=broad-except

        if request.method == 'POST':
            form = CSVImportForm(request.POST, request.FILES)
            if form.is_valid():
                try:
                    agencies = AgencyCsvModel.import_data(data=request.FILES['csv_file'],
                                                          extra_fields=['True'])
                    messages.success(request, 'CSV - %d agencies imported' % len(agencies))
                except Exception as exc:
                    messages.error(request, 'ERROR: %s' % str(exc))
                    logger.error('Import error: %s', exc, exc_info=sys.exc_info())
                else:
                    if form.cleaned_data['type_']:
                        for agency in agencies:
                            agency.object.types.add(form.cleaned_data['type_'])
                    for agency in agencies:
                        aobj = agency.object
                        if not aobj.slug:
                            aobj.slug = slugify(aobj.name)
                            aobj.save()
                return redirect('admin:agency_agency_changelist')
        else:
            form = CSVImportForm()

        fields = ['name', 'slug', 'jurisdiction ("Boston, MA")', 'address', 'email', 'other_emails',
                  'contact first name', 'contact last name', 'contact_title', 'url', 'phone', 'fax']
        return render(
                request,
                'admin/agency/import.html',
                {'form': form, 'fields': fields},
                )


admin.site.register(AgencyType, AgencyTypeAdmin)
admin.site.register(Agency, AgencyAdmin)


def get_jurisdiction(full_name):
    """Get the jurisdiction from its name and parent"""
    if ', ' in full_name:
        name, parent_abbrev = full_name.split(', ')
        parent = Jurisdiction.objects.get(abbrev=parent_abbrev)
        return Jurisdiction.objects.get(name=name, parent=parent).pk
    else:
        return Jurisdiction.objects.exclude(level='l').get(name=full_name).pk

class EmailValidator(object):
    """Class to validate emails"""
    def validate(self, value):
        # pylint: disable=no-self-use
        """Must be blank or an email"""
        if value == '':
            return True
        # validate email will throw a validation error on failure
        validate_email(value)
        return True

class AgencyCsvModel(CsvModel):
    """CSV import model for agency"""

    name = CharField()
    slug = CharField()
    jurisdiction = DjangoModelField(Jurisdiction, prepare=get_jurisdiction)
    address = CharField()
    email = CharField(validator=EmailValidator)
    other_emails = CharField()
    contact_first_name = CharField()
    contact_last_name = CharField()
    contact_title = CharField()
    url = CharField()
    phone = CharField()
    fax = CharField()
    status = CharField()

    class Meta:
        # pylint: disable=too-few-public-methods
        dbModel = Agency
        delimiter = ','
        update = {'keys': ['name', 'jurisdiction']}
