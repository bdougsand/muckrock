# -*- coding: utf-8 -*-
"""
Models for keeping track of how we send and receive communications
"""

from django.db import models

from email.utils import parseaddr, getaddresses

from localflavor.us.us_states import STATE_CHOICES
from phonenumber_field.modelfields import PhoneNumberField


PHONE_TYPES = (
        ('fax', 'Fax',),
        ('phone', 'Phone'),
        )


# Address models

class EmailAddressQuerySet(models.QuerySet):
    """QuerySet for EmailAddresses"""

    def fetch(self, address):
        """Fetch an email address object based on an email header"""
        name, email = parseaddr(address)
        email = self._normalize_email(email)
        email_address, _ = self.update_or_create(
                email=email,
                defaults={'name': name},
                )
        return email_address

    def fetch_many(self, *addresses):
        """Fetch multiple email address objects based on an email header"""
        name_emails = getaddresses(addresses)
        addresses = []
        for name, email in name_emails:
            email = self._normalize_email(email)
            email_address, _ = self.update_or_create(
                    email=email,
                    defaults={'name': name},
                    )
            addresses.append(email_address)
        return addresses

    @staticmethod
    def _normalize_email(email):
        """Username is case sensitive, domain is not"""
        if '@' not in email:
            # XXX validate email
            pass
        username, domain = email.rsplit('@', 1)
        return '%s@%s' % (username, domain.lower())


class EmailAddress(models.Model):
    """An email address"""
    email = models.EmailField(unique=True)
    name = models.CharField(blank=True)
    # XXX concept of known good / known spam / known invalid email address?

    objects = EmailAddressQuerySet.as_manager()

    def __unicode__(self):
        if self.name:
            return "%s <%s>" % (self.name, self.email)
        else:
            return self.email

    @property
    def domain(self):
        """The domain part of the email address"""
        if '@' not in self.email:
            return ''
        return self.email.rsplit('@', 1)[1]

    def allowed(self, foia=None):
        """Is this email address allowed to post to this FOIA request?"""
        # XXX ensure this is tested

        allowed_tlds = ['.%s.us' % a.lower() for (a, _) in STATE_CHOICES
                if a not in ('AS', 'DC', 'GU', 'MP', 'PR', 'VI')]
        allowed_tlds.extend(['.gov', '.mil'])
        
        # from the same domain as the FOIA email
        if foia and foia.email and self.domain == foia.email.domain:
            return True

        # the email is a known email for this FOIA's agency
        if foia and self.agencies.filter(pk=foia.agency_id).exists():
            return True

        # the email is a known email for this FOIA
        if foia and foia.cc_emails.filter(email=self).exists():
            return True

        # it is from any known government TLD
        if any(self.email.endswith(tld) for tld in allowed_tlds):
            return True

        # if not associated with any FOIA,
        # checked if the email is known for any agency
        if not foia and AgencyEmail.objects.filter(email=self).exists():
            return True

        # check the email domain against the whitelist
        if WhitelistDomain.objects.filter(domain__iexact=self.domain).exists():
            return True

        return False


class PhoneNumber(models.Model):
    """A phone number"""
    number = PhoneNumberField(unique=True)
    type = models.CharField(max_length=5, choices=PHONE_TYPES)
    # XXX extension?

    def __unicode__(self):
        return '%s (%s)' % (self.phone, self.type)


class Address(models.Model):
    """A mailing address"""
    # XXX how do we want to do this?
    # uniqify this
    address = models.TextField(blank=True)

    # contact - field? model?
    street = models.CharField(blank=True)
    city = models.CharField(blank=True)
    state = models.CharField(blank=True) # XXX use usa state field
    zip_code = models.CharField(blank=True) # XXX use usa zip field
    country = models.CharField(blank=True) # XXX use a country field?
    # XXX put a geolocated point field here?

    def __unicode__(self):
        return self.address


# Communication models

class EmailCommunication(models.Model):
    """An email sent or received to deliver a communication"""
    communication = models.ForeignKey('foia.FOIACommunication', related='emails')
    sent_datetime = models.DateTimeField()
    confirmed_datetime = models.DateTimeField(blank=True, null=True)
    from_email = models.ForeignKey(EmailAddress, blank=True, null=True)
    to_emails = models.ManyToMany(EmailAddress)
    cc_emails = models.ManyToManyField(EmailAddress)
    bcc_emails = models.ManyToManyField(EmailAddress)

    def __unicode__(self):
        value = 'Email Communication'
        if self.from_email:
            value += ' From: "%s"' % self.from_email
        if self.to_email:
            value += ' To: "%s"' % self.to_email
        return value

    def set_raw_email(self, msg):
        """Set the raw email for this communication"""
        from muckrock.foia.models import RawEmail
        raw_email = RawEmail.objects.get_or_create(email=self)[0]
        raw_email.raw_email = msg
        raw_email.save()


class FaxCommunication(models.Model):
    """A fax sent to deliver a communication"""
    communication = models.ForeignKey('foia.FOIACommunication', related='faxes')
    sent_datetime = models.DateTimeField()
    confirmed_datetime = models.DateTimeField(blank=True, null=True)
    to_number = models.ForeignKey(PhoneNumber)
    fax_id = models.CharField(max_length=10, blank=True, default='')

    def __unicode__(self):
        return 'Fax Communication To %s' % self.to_number


class MailCommunication(models.Model):
    """A snail mail sent or received to deliver a communication"""
    communication = models.ForeignKey('foia.FOIACommunication', related='mails')
    sent_datetime = models.DateTimeField()
    from_address = models.ForeignKey(Address, blank=True, null=True)
    to_address = models.ForeignKey(Address, blank=True, null=True)

    def __unicode__(self):
        return 'Mail Communication To %s' % self.to_address


class WebCommunication(models.Model):
    """A communication posted to our site directly through our web form"""
    communication = models.ForeignKey('foia.FOIACommunication', related='web_comms')
    sent_datetime = models.DateTimeField()

    def __unicode__(self):
        return 'Web Communication'


# Error models
class EmailError(models.Model):
    """An error has occured delivering this email"""
    email = models.ForeignKey(
            'communication.EmailCommunication',
            related_name='errors',
            )
    datetime = models.DateTimeField()

    recipient = models.ForeignKey(
            'communication.EmailAddress',
            related_name='errors',
            )
    code = models.CharField(max_length=10)
    error = models.TextField(blank=True)
    event = models.CharField(max_length=10)
    reason = models.CharField(max_length=255)

    def __unicode__(self):
        return u'Email Error: %s - %s' % (self.email.pk, self.datetime)

    class Meta:
        ordering = ['datetime']
        app_label = 'foia'


class FaxError(models.Model):
    """An error has occured delivering this fax"""
    fax = models.ForeignKey(
            'communication.FaxCommunication',
            related_name='errors',
            )
    datetime = models.DateTimeField()

    recipient = models.ForeignKey(
            'communication.PhoneNumber',
            related_name='errors',
            )
    error_type = models.CharField(blank=True, max_length=255)
    error_code = models.CharField(blank=True, max_length=255)
    error_id = models.SmallPositiveInteger(blank=True, null=True)

    def __unicode__(self):
        return u'Fax Error: %s - %s' % (self.fax.pk, self.datetime)

    class Meta:
        ordering = ['datetime']
        app_label = 'foia'


