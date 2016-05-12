# -*- coding: utf-8 -*-
"""
Models for the FOIA application
"""

import datetime

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.validators import validate_email
from django.db import models
from django.shortcuts import get_object_or_404

import logging

from muckrock.foia.models.request import FOIARequest, STATUS
from muckrock.utils import unique_username

logger = logging.getLogger(__name__)

DELIVERED = (
    ('fax', 'Fax'),
    ('email', 'Email'),
    ('mail', 'Mail'),
    ('web', 'Web'), # The agency uploaded their reponse through our web interface
)

class FOIACommunication(models.Model):
    """A single communication of a FOIA request"""

    foia = models.ForeignKey(FOIARequest, related_name='communications', blank=True, null=True)

    # XXX these become deprecated
    # from/to names
    from_who = models.CharField(max_length=255)
    to_who = models.CharField(max_length=255, blank=True)
    # from/to full email address
    priv_from_who = models.CharField(max_length=255, blank=True)
    priv_to_who = models.CharField(max_length=255, blank=True)
    # XXX

    from_user = models.ForeignKey(
            'auth.User',
            blank=True,
            null=True,
            related_name='comms_from',
            on_delete=models.PROTECT,
            )
    to_user = models.ForeignKey(
            'auth.User',
            blank=True,
            null=True,
            related_name='comms_to',
            on_delete=models.PROTECT,
            )

    subject = models.CharField(max_length=255, blank=True)
    date = models.DateTimeField(db_index=True)
    response = models.BooleanField(default=False,
            help_text='Is this a response (or a request)?')
    autogenerated = models.BooleanField(default=False)
    thanks = models.BooleanField(default=False)
    full_html = models.BooleanField(default=False) # XXX dont need to specify the deault everywhere
    communication = models.TextField(blank=True)
    delivered = models.CharField(max_length=10, choices=DELIVERED, blank=True, null=True)
    # what status this communication should set the request to - used for machine learning
    status = models.CharField(max_length=10, choices=STATUS, blank=True, null=True)

    # confirmed, opened, error
    # for outgoing messages (not responses)
    #
    # confirmed:
    #  - email: mailgun delivered it
    #  - fax: faxaway confirms it sent
    #  - snail: snailmail task is closed (it was placed in the mail)
    #
    # opened:
    #  - email: mailgun registered an open event (this comes with lots of metadata)
    #
    # error:
    #  - email: mailgun had an error (hard/soft, error msg)
    #  - fax: faxaway failed to fax the message (error msg)

    opened = models.BooleanField(default=False,
            help_text='If emailed, did we receive an open notification? '
                      'If faxed, did we recieve a confirmation?')

    # only used for orphans
    # XXX approved bool instead of seprate FK?
    likely_foia = models.ForeignKey(
        FOIARequest,
        related_name='likely_communications',
        blank=True,
        null=True
    )

    reindex_related = ('foia',)

    def __unicode__(self):
        if self.delivered:
            if self.delivered == 'email':
                return u'an email'
            elif self.delivered == 'mail':
                return u'a letter'
            else:
                return u'a fax'
        else:
            return u'a message'

    def get_absolute_url(self):
        """The url for this object"""
        return self.foia.get_absolute_url() + ('#comm-%d' % self.pk)

    def save(self, *args, **kwargs):
        """Remove controls characters from text before saving"""
        remove_control = dict.fromkeys(range(0, 9) + range(11, 13) + range(14, 32))
        self.communication = unicode(self.communication).translate(remove_control)
        # limit communication length to 150k
        self.communication = self.communication[:150000]
        # special handling for certain agencies
        self._presave_special_handling()
        # update foia's date updated if this is the latest communication
        if (self.foia and
                (self.foia.date_updated is None or
                 self.date.date() > self.foia.date_updated)):
            self.foia.date_updated = self.date.date()
            self.foia.save(comment='update date_updated due to new comm')
        super(FOIACommunication, self).save(*args, **kwargs)

    def anchor(self):
        """Anchor name"""
        return 'comm-%d' % self.pk

    def move(self, foia_pks):
        """
        Move this communication. If more than one foia_pk is given, move the
        communication to the first request, then clone it across the rest of
        the requests. Returns the moved and cloned communications.
        """
        if not foia_pks:
            raise ValueError('Expected a request to move the communication to.')
        if not isinstance(foia_pks, list):
            foia_pks = [foia_pks]
        move_to_request = get_object_or_404(FOIARequest, pk=foia_pks[0])
        self.foia = move_to_request
        for each_file in self.files.all():
            each_file.foia = move_to_request
            each_file.save()
        self.save()
        logging.info('Communication #%d moved to request #%d', self.id, self.foia.id)
        # if cloning happens, self gets overwritten. so we save it to a variable here
        this_comm = FOIACommunication.objects.get(pk=self.pk)
        moved = [this_comm]
        cloned = []
        if foia_pks[1:]:
            cloned = self.clone(foia_pks[1:])
        return moved + cloned

    def clone(self, foia_pks):
        """
        Copies the communication to each request in the list,
        then returns all the new communications.
        ---
        When setting self.pk to None and then calling self.save(),
        Django will clone the communication along with all of its data
        and give it a new primary key. On the next iteration of the loop,
        the clone will be cloned along with its data, and so on. Same thing
        goes for each file attached to the communication.
        """
        # avoid circular imports
        from muckrock.foia.tasks import upload_document_cloud
        if not isinstance(foia_pks, list):
            foia_pks = [foia_pks]
        request_list = FOIARequest.objects.filter(pk__in=foia_pks)
        if not request_list:
            raise ValueError('No valid request(s) provided for cloning.')
        cloned_comms = []
        original_pk = self.pk
        files = self.files.all()
        for request in request_list:
            this_clone = FOIACommunication.objects.get(pk=original_pk)
            this_clone.pk = None
            this_clone.foia = request
            this_clone.save()
            for file_ in files:
                original_file_id = file_.id
                file_.pk = None
                file_.foia = request
                file_.comm = this_clone
                # make a copy of the file on the storage backend
                try:
                    new_ffile = ContentFile(file_.ffile.read())
                except ValueError:
                    error_msg = ('FOIAFile #%s has no data in its ffile field. '
                                'It has not been cloned.')
                    logging.error(error_msg, original_file_id)
                    continue
                new_ffile.name = file_.ffile.name
                file_.ffile = new_ffile
                file_.save()
                upload_document_cloud.apply_async(args=[file_.pk, False], countdown=3)
            # for each clone, self gets overwritten. each clone needs to be stored explicitly.
            cloned_comms.append(this_clone)
            logging.info('Communication #%d cloned to request #%d', original_pk, this_clone.foia.id)
        return cloned_comms

    def resend(self, email_address=None):
        """Resend the communication"""
        foia = self.foia
        if not foia:
            logging.error('Tried resending an orphaned communication.')
            raise ValueError('This communication has no FOIA to submit.', 'no_foia')
        if not foia.agency or not foia.agency.status == 'approved':
            logging.error('Tried resending a communication with an unapproved agency')
            raise ValueError('This communication has no approved agency.', 'no_agency')
        snail = False
        self.date = datetime.datetime.now()
        if email_address:
            # responsibility for handling validation errors
            # is on the caller of the resend method
            validate_email(email_address)
            user, _ = User.objects.get_or_create(
                    email=email_address,
                    defaults={'username': unique_username(email_address)},
                    )
            self.to_user = user
            foia.contact = user
            foia.save(comment='new email from comm resend')
        else:
            snail = True
        self.save()
        foia.submit(snail=snail)
        logging.info('Communication #%d resent.', self.id)

    def set_raw_email(self, msg):
        """Set the raw email for this communication"""
        raw_email = RawEmail.objects.get_or_create(communication=self)[0]
        raw_email.raw_email = msg
        raw_email.save()

    def make_sender_primary_contact(self):
        """Makes the communication's sender the primary email of its FOIA."""
        if not self.foia:
            raise ValueError('Communication is an orphan and has no associated request.')
        self.foia.contact = self.from_user
        self.foia.save(comment='update primary email from comm')

    def _presave_special_handling(self):
        """Special handling before saving
        For example, strip out BoP excessive quoting"""

        def test_agency_name(name):
            """Match on agency name"""
            return (self.foia and self.foia.agency and
                    self.foia.agency.name == name)

        def until_string(string):
            """Cut communication off after string"""
            def modify():
                """Run the modification on self.communication"""
                if string in self.communication:
                    idx = self.communication.index(string)
                    self.communication = self.communication[:idx]
            return modify

        special_cases = [
            # BoP: strip everything after '>>>'
            (test_agency_name('Bureau of Prisons'),
             until_string('>>>')),
            # Phoneix Police: strip everything after '_'*32
            (test_agency_name('Phoenix Police Department'),
             until_string('_' * 32)),
        ]

        for test, modify in special_cases:
            if test:
                modify()


    class Meta:
        # pylint: disable=too-few-public-methods
        ordering = ['date']
        verbose_name = 'FOIA Communication'
        app_label = 'foia'


class RawEmail(models.Model):
    """The raw email text for a communication - stored seperately for performance"""

    communication = models.OneToOneField(FOIACommunication)
    raw_email = models.TextField(blank=True)

    def __unicode__(self):
        return 'Raw Email: %d' % self.pk

    class Meta:
        app_label = 'foia'


class FOIANote(models.Model):
    """A private note on a FOIA request"""

    foia = models.ForeignKey(FOIARequest, related_name='notes')
    author = models.ForeignKey('auth.User', related_name='notes', null=True)
    datetime = models.DateTimeField(auto_now_add=True)
    note = models.TextField()

    def __unicode__(self):
        # pylint: disable=redefined-variable-type
        if self.author:
            user = self.author
        else:
            user = self.foia.user
        return 'Note by %s on %s' % (user.get_full_name(), self.foia.title)

    class Meta:
        # pylint: disable=too-few-public-methods
        ordering = ['foia', 'datetime']
        verbose_name = 'FOIA Note'
        app_label = 'foia'
