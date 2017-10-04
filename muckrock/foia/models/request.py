# -*- coding: utf-8 -*-
"""
Models for the FOIA application
"""

from django.conf import settings
from django.contrib.auth.models import User, AnonymousUser
from django.core.mail import EmailMultiAlternatives
from django.core.urlresolvers import reverse
from django.db import models, connection
from django.db.models import Q, Sum, Count, Max, Case, When
from django.template.defaultfilters import escape, linebreaks, slugify
from django.template.loader import render_to_string

from actstream.models import followers
import chardet
from datetime import datetime, date, timedelta
from hashlib import md5
import logging
import mimetypes
import os.path
from reversion import revisions as reversion
from taggit.managers import TaggableManager

from muckrock.accounts.models import Notification
from muckrock.communication.models import (
        EmailAddress,
        PhoneNumber,
        Address,
        EmailCommunication,
        FaxCommunication,
        MailCommunication,
        )
from muckrock.models import ExtractDay, Now
from muckrock.tags.models import Tag, TaggedItemBase, parse_tags
from muckrock import task
from muckrock import fields
from muckrock import utils

logger = logging.getLogger(__name__)

class FOIARequestQuerySet(models.QuerySet):
    """Object manager for FOIA requests"""
    # pylint: disable=too-many-public-methods

    def get_submitted(self):
        """Get all submitted FOIA requests"""
        return self.exclude(status='started')

    def get_done(self):
        """Get all FOIA requests with responses"""
        return self.filter(status__in=['partial', 'done']).exclude(date_done=None)

    def get_editable(self):
        """Get all editable FOIA requests"""
        return self.filter(status='started')

    def get_viewable(self, user):
        """Get all viewable FOIA requests for given user"""

        if user.is_staff:
            return self.all()

        if user.is_authenticated():
            # Requests are visible if you own them, have view or edit permissions,
            # or if they are not drafts and not embargoed
            query = (Q(user=user) |
                    Q(edit_collaborators=user) |
                    Q(read_collaborators=user) |
                    (~Q(status='started') & ~Q(embargo=True)))
            # agency users may also view requests for their agency
            if user.profile.acct_type == 'agency':
                query = query | Q(agency=user.profile.agency)
            # organizational users may also view requests from their org that are shared
            if user.profile.organization is not None:
                query = query | Q(
                        user__profile__org_share=True,
                        user__profile__organization=user.profile.organization,
                        )
            return self.filter(query)
        else:
            # anonymous user, filter out drafts and embargoes
            return (self.exclude(status='started')
                        .exclude(embargo=True))

    def get_public(self):
        """Get all publically viewable FOIA requests"""
        return self.get_viewable(AnonymousUser())

    def get_overdue(self):
        """Get all overdue FOIA requests"""
        return self.filter(status__in=['ack', 'processed'], date_due__lt=date.today())

    def get_manual_followup(self):
        """Get old requests which require us to follow up on with the agency"""

        return [
            f for f in self.get_overdue()
            if f.communications.all().reverse()[0].date + timedelta(15) < datetime.now()
        ]

    def get_followup(self):
        """Get requests that need follow up emails sent"""
        return self.filter(status__in=['ack', 'processed'],
                           date_followup__lte=date.today(),
                           disable_autofollowups=False)

    def get_open(self):
        """Get requests which we are awaiting a response from"""
        return self.filter(status__in=['ack', 'processed', 'appealing'])

    def get_undated(self):
        """Get requests which have an undated file"""
        return self.filter(~Q(files=None) & Q(files__date=None)).distinct()

    def organization(self, organization):
        """Get requests belonging to an organization's members."""
        return (self.select_related(
                        'agency',
                        'jurisdiction',
                        'jurisdiction__parent',
                        'jurisdiction__parent__parent')
                    .filter(user__profile__organization=organization)
                    .exclude(status='started')
                    .order_by('-date_submitted'))

    def select_related_view(self):
        """Select related models for viewing"""
        return self.select_related(
            'agency',
            'agency__jurisdiction',
            'jurisdiction',
            'jurisdiction__parent',
            'jurisdiction__parent__parent',
            'user',
            'crowdfund',
        )

    def get_public_file_count(self, limit=None):
        """Annotate the public file count"""
        foia_qs = self
        count_qs = (self._clone()
                .values_list('id')
                .filter(files__access='public')
                .annotate(Count('files'))
                )
        if limit is not None:
            foia_qs = foia_qs[:limit]
            count_qs = count_qs[:limit]
        counts = dict(count_qs)
        foias = []
        for foia in foia_qs:
            foia.public_file_count = counts.get(foia.pk, 0)
            foias.append(foia)
        return foias

    def get_stale(self, agency=None):
        """Load requests for a stale agency"""
        foia_qs = (self
                .get_open()
                .annotate(
                    latest_response=ExtractDay(
                        Now() - Max(Case(When(
                            communications__response=True,
                            then='communications__date'
                            )))))
                .order_by('-latest_response')
                .select_related('jurisdiction')
                )
        if agency is not None:
            foia_qs = foia_qs.filter(agency=agency)
        return foia_qs


STATUS = [
    ('started', 'Draft'),
    ('submitted', 'Processing'),
    ('ack', 'Awaiting Acknowledgement'),
    ('processed', 'Awaiting Response'),
    ('appealing', 'Awaiting Appeal'),
    ('fix', 'Fix Required'),
    ('payment', 'Payment Required'),
    ('lawsuit', 'In Litigation'),
    ('rejected', 'Rejected'),
    ('no_docs', 'No Responsive Documents'),
    ('done', 'Completed'),
    ('partial', 'Partially Completed'),
    ('abandoned', 'Withdrawn'),
]

END_STATUS = ['rejected', 'no_docs', 'done', 'partial', 'abandoned']

class Action():
    """A helper class to provide interfaces for request actions"""
    # pylint: disable=too-many-arguments
    def __init__(self, test=None, link=None, title=None, action=None, desc=None, class_name=None):
        self.test = test
        self.link = link
        self.title = title
        self.action = action
        self.desc = desc
        self.class_name = class_name

    def is_possible(self):
        """Is this action possible given the current context?"""
        return self.test


class FOIARequest(models.Model):
    """A Freedom of Information Act request"""
    # pylint: disable=too-many-public-methods
    # pylint: disable=too-many-instance-attributes

    user = models.ForeignKey(User)
    title = models.CharField(max_length=255, db_index=True)
    slug = models.SlugField(max_length=255)
    status = models.CharField(max_length=10, choices=STATUS, db_index=True)
    jurisdiction = models.ForeignKey('jurisdiction.Jurisdiction')
    agency = models.ForeignKey('agency.Agency', blank=True, null=True)
    date_submitted = models.DateField(blank=True, null=True, db_index=True)
    date_updated = models.DateField(blank=True, null=True, db_index=True)
    date_done = models.DateField(blank=True, null=True, verbose_name='Date response received')
    date_due = models.DateField(blank=True, null=True, db_index=True)
    days_until_due = models.IntegerField(blank=True, null=True)
    date_followup = models.DateField(blank=True, null=True)
    date_estimate = models.DateField(blank=True, null=True,
            verbose_name='Estimated Date Completed')
    date_processing = models.DateField(blank=True, null=True)
    embargo = models.BooleanField(default=False)
    permanent_embargo = models.BooleanField(default=False)
    date_embargo = models.DateField(blank=True, null=True)
    price = models.DecimalField(max_digits=14, decimal_places=2, default='0.00')
    requested_docs = models.TextField(blank=True)
    description = models.TextField(blank=True)
    featured = models.BooleanField(default=False)
    tracker = models.BooleanField(default=False)
    sidebar_html = models.TextField(blank=True)
    tracking_id = models.CharField(blank=True, max_length=255)
    mail_id = models.CharField(blank=True, max_length=255, editable=False)
    updated = models.BooleanField(default=False)

    # XXX dont just assume we are sending to email anymore
    email = models.CharField(blank=True, max_length=254)
    other_emails = fields.EmailsListField(blank=True, max_length=255)
    # new fields
    # XXX name claseh
    email = models.ForeignKey(
            'communication.EmailAddress',
            related_name='foias',
            )
    cc_emails = models.ManyToManyField(
            'communication.EmailAddress',
            related_name='cc_foias',
            )
    fax = models.ForeignKey(
            'communication.PhoneNumber',
            related_name='foias',
            )
    address = models.ForeignKey(
            'communication.Address',
            related_name='foias',
            )

    times_viewed = models.IntegerField(default=0)
    disable_autofollowups = models.BooleanField(default=False)
    missing_proxy = models.BooleanField(default=False,
            help_text='This request requires a proxy to file, but no such '
            'proxy was avilable upon draft creation.')
    parent = models.ForeignKey('self', blank=True, null=True, on_delete=models.SET_NULL)
    block_incoming = models.BooleanField(
        default=False,
        help_text=('Block emails incoming to this request from '
                   'automatically being posted on the site')
    )
    crowdfund = models.OneToOneField('crowdfund.Crowdfund',
            related_name='foia', blank=True, null=True)
    multirequest = models.ForeignKey(
            'foia.FOIAMultiRequest',
            blank=True,
            null=True,
            )

    read_collaborators = models.ManyToManyField(
        User,
        related_name='read_access',
        blank=True,
    )
    edit_collaborators = models.ManyToManyField(
        User,
        related_name='edit_access',
        blank=True,
    )
    access_key = models.CharField(blank=True, max_length=255)

    objects = FOIARequestQuerySet.as_manager()
    tags = TaggableManager(through=TaggedItemBase, blank=True)

    foia_type = 'foia'

    def __unicode__(self):
        return self.title

    def get_absolute_url(self):
        """The url for this object"""
        return reverse(
                'foia-detail',
                kwargs={
                    'jurisdiction': self.jurisdiction.slug,
                    'jidx': self.jurisdiction.pk,
                    'slug': self.slug,
                    'idx': self.pk,
                    })

    def save(self, *args, **kwargs):
        """Normalize fields before saving and set the embargo expiration if necessary"""
        self.slug = slugify(self.slug)
        self.title = self.title.strip()
        if self.embargo:
            if self.status in END_STATUS:
                default_date = date.today() + timedelta(30)
                existing_date = self.date_embargo
                self.date_embargo = default_date if not existing_date else existing_date
            else:
                self.date_embargo = None
        if self.status == 'submitted' and self.date_processing is None:
            self.date_processing = date.today()

        # add a reversion comment if possible
        if 'comment' in kwargs:
            comment = kwargs.pop('comment')
            if reversion.revision_context_manager.is_active():
                reversion.set_comment(comment)
        super(FOIARequest, self).save(*args, **kwargs)

    def is_editable(self):
        """Can this request be updated?"""
        return self.status == 'started'

    def has_crowdfund(self):
        """Does this request have crowdfunding enabled?"""
        return bool(self.crowdfund)

    def is_payable(self):
        """Can this request be payed for by the user?"""
        has_open_crowdfund = self.has_crowdfund() and not self.crowdfund.expired()
        has_payment_status = self.status == 'payment'
        return has_payment_status and not has_open_crowdfund

    def get_stripe_amount(self):
        """Output a Stripe Checkout formatted price"""
        return int(self.price*100)

    def is_public(self):
        """Is this document viewable to everyone"""
        return self.has_perm(AnonymousUser(), 'view')

    # Request Sharing and Permissions

    def has_perm(self, user, perm):
        """Short cut for checking a FOIA permission"""
        return user.has_perm('foia.%s_foiarequest' % perm, self)

    ## Creator

    def created_by(self, user):
        """Did this user create this request?"""
        return self.user == user

    ## Editors

    def has_editor(self, user):
        """Checks whether the given user is an editor."""
        user_is_editor = False
        if self.edit_collaborators.filter(pk=user.pk).exists():
            user_is_editor = True
        return user_is_editor

    def add_editor(self, user):
        """Grants the user permission to edit this request."""
        if not self.has_viewer(user) and not self.has_editor(user) and not self.created_by(user):
            self.edit_collaborators.add(user)
            self.save()
            logger.info('%s granted edit access to %s', user, self)
        return

    def remove_editor(self, user):
        """Revokes the user's permission to edit this request."""
        if self.has_editor(user):
            self.edit_collaborators.remove(user)
            self.save()
            logger.info('%s revoked edit access from %s', user, self)
        return

    def demote_editor(self, user):
        """Reduces the editor's access to that of a viewer."""
        self.remove_editor(user)
        self.add_viewer(user)
        return

    ## Viewers

    def has_viewer(self, user):
        """Checks whether the given user is a viewer."""
        user_is_viewer = False
        if self.read_collaborators.filter(pk=user.pk).exists():
            user_is_viewer = True
        return user_is_viewer

    def add_viewer(self, user):
        """Grants the user permission to view this request."""
        if not self.has_viewer(user) and not self.has_editor(user) and not self.created_by(user):
            self.read_collaborators.add(user)
            self.save()
            logger.info('%s granted view access to %s', user, self)
        return

    def remove_viewer(self, user):
        """Revokes the user's permission to view this request."""
        if self.has_viewer(user):
            self.read_collaborators.remove(user)
            logger.info('%s revoked view access from %s', user, self)
            self.save()
        return

    def promote_viewer(self, user):
        """Enhances the viewer's access to that of an editor."""
        self.remove_viewer(user)
        self.add_editor(user)
        return

    ## Access key

    def generate_access_key(self):
        """Generates a random key for accessing the request when it is private."""
        key = utils.generate_key(24)
        self.access_key = key
        self.save()
        logger.info('New access key generated for %s', self)
        return key

    def public_documents(self):
        """Get a list of public documents attached to this request"""
        return self.files.filter(access='public')

    def first_request(self):
        """Return the first request text"""
        try:
            return self.communications.all()[0].communication
        except IndexError:
            return ''

    def last_comm(self):
        """Return the last communication"""
        return self.communications.last()

    def last_response(self):
        """Return the most recent response"""
        return self.communications.filter(response=True).order_by('-date').first()

    def set_mail_id(self):
        """Set the mail id, which is the unique identifier for the auto mailer system"""
        # use raw sql here in order to avoid race conditions
        uid = int(md5(self.title.encode('utf8') +
                      datetime.now().isoformat()).hexdigest(), 16) % 10 ** 8
        mail_id = '%s-%08d' % (self.pk, uid)
        cursor = connection.cursor()
        cursor.execute("UPDATE foia_foiarequest "
                       "SET mail_id = CASE WHEN mail_id='' THEN %s ELSE mail_id END "
                       "WHERE id = %s", [mail_id, self.pk])
        # set object's mail id to what is in the database
        self.mail_id = FOIARequest.objects.get(pk=self.pk).mail_id

    def get_mail_id(self):
        """Get the mail id - generate it if it doesn't exist"""
        if not self.mail_id:
            self.set_mail_id()
        return self.mail_id

    def get_other_emails(self):
        """Get the other emails for this request as a list"""
        return self.cc_emails.all()

    def get_to_user(self):
        """Who communications are to"""
        if self.agency:
            return self.agency.get_user()
        else:
            return None

    def get_saved(self):
        """Get the old model that is saved in the db"""
        try:
            return FOIARequest.objects.get(pk=self.pk)
        except FOIARequest.DoesNotExist:
            return None

    def latest_response(self):
        """How many days since the last response"""
        response = self.last_response()
        if response:
            return (date.today() - response.date.date()).days

    def processing_length(self):
        """How many days since the request was set as processing"""
        days_since = 0
        if self.date_processing:
            days_since = (date.today() - self.date_processing).days
        return days_since

    def update(self, anchor=None):
        """Various actions whenever the request has been updated"""
        # pylint: disable=unused-argument
        # Do something with anchor
        self.updated = True
        self.save()
        self.update_dates()

    def notify(self, action):
        """
        Notify the owner of the request.
        Notify followers if the request is not under embargo.
        Mark any existing notifications with the same message as read,
        to avoid notifying users with duplicated information.
        """
        identical_notifications = (Notification.objects.for_object(self).get_unread()
            .filter(action__actor_object_id=action.actor_object_id, action__verb=action.verb))
        for notification in identical_notifications:
            notification.mark_read()
        utils.notify(self.user, action)
        if self.is_public():
            utils.notify(followers(self), action)

    def submit(self, appeal=False, snail=False, thanks=False):
        """
        The request has been submitted.
        Notify admin and try to auto submit.
        There is functionally no difference between appeals and other submissions
        besides the receiving agency.
        The only difference between a thanks andother submissions is that we do
        not set the request status, unless the request requires a proxy.
        """
        if not self.agency:
            # XXX this should not happen
            # XXX make sure we catch this
            raise ValueError('Trying to submit a request without an agency.')

        if appeal and self.agency.appeal_agency:
            agency = self.agency.appeal_agency
        else:
            agency = self.agency

        self.update_current_address(agency, appeal)

        # if agency isnt approved, do not email or snail mail
        # it will be handled after agency is approved
        approved_agency = agency.status == 'approved'

        if self.missing_proxy:
            self._flag_proxy_resubmit()
        elif not approved_agency:
            # not an approved agency, all we do is mark as submitted
            self.status = 'submitted'
            self.date_processing = date.today()
        else:
            self._send_msg(appeal=appeal, thanks=thanks, snail=snail)
            self.update_dates()
        self.save()

    def update_current_address(self, agency, appeal):
        """Update the current address for the request"""
        # if this is an appeal, clear the current addresses and get them
        # from the appeal agency
        if appeal:
            self.email = None
            self.cc_emails.clear()
            self.fax = None
            self.address = None
            request_type = 'appeal'
        else:
            request_type = 'primary'
        # if no addresses are set, pull them from the agency
        if not self.email and not self.fax and not self.address:
            self.email = agency.get_emails(request_type, 'to').first()
            self.cc_emails.set(agency.get_emails(request_type, 'cc'))
            self.fax = agency.get_fax(request_type)
            self.address = agency.get_address(request_type)
        self.save()

    def _flag_proxy_resubmit(self):
        """Flag this request to be re-submitted with a proxy"""
        self.status = 'submitted'
        self.date_processing = date.today()
        task.models.FlaggedTask.objects.create(
                foia=self,
                text='This request was filed for an agency requiring a '
                'proxy, but no proxy was available.  Please add a suitable '
                'proxy for the state and refile it with a note that the '
                'request is being filed by a state citizen. Make sure the '
                'new request is associated with the original user\'s '
                'account. To add someone as a proxy, change their user type '
                'to "Proxy" and make sure they properly have their state '
                'set on the backend.  This message should only appear when '
                'a suitable proxy does not exist.'
                )

    def process_attachments(self, user):
        """Attach all outbound attachments to the last communication"""
        attachments = self.pending_attachments.filter(
                user=user,
                sent=False,
                )
        comm = self.last_comm()
        access = 'private' if self.embargo else 'public'
        for attachment in attachments:
            file_ = comm.files.create(
                    foia=self,
                    title=os.path.basename(attachment.ffile.name),
                    date=comm.date,
                    source=user.get_full_name(),
                    access=access,
                    )
            file_.ffile.name = attachment.ffile.name
            file_.save()
        attachments.update(sent=True)

    def followup(self, automatic=False, show_all_comms=True):
        """Send a follow up email for this request"""
        if self.date_estimate and date.today() < self.date_estimate:
            estimate = 'future'
        elif self.date_estimate:
            estimate = 'past'
        else:
            estimate = 'none'

        self.communications.create(
            from_user=user.objects.get(username='MuckrockStaff'), # XXX ensure muckrock user exists
            to_who=self.get_to_user(),
            date=datetime.now(),
            response=False,
            autogenerated=automatic,
            communication=render_to_string(
                'text/foia/followup.txt',
                {'request': self, 'estimate': estimate}
                ))

        self.submit(followup=True)

    def appeal(self, appeal_message, user):
        """Send a followup to the agency or its appeal agency."""
        communication = self.communications.create(
            from_user=user,
            to_who=self.get_to_user(),
            date=datetime.now(),
            communication=appeal_message,
            response=False,
        )
        self.process_attachments(user)
        self.submit(appeal=True)
        return communication

    def pay(self, user, amount):
        """
        Users can make payments for request fees.
        Upon payment, we create a snail mail task and we set the request to a processing status.
        Payments are always snail mail, because we need to mail the check to the agency.
        Since collaborators may make payments, we do not assume the user is the request creator.
        Returns the communication that was generated.
        """
        # We create the payment communication and a snail mail task for it.
        payable_to = self.agency.payable_to if self.agency else None
        comm = self.communications.create(
            from_user=User.objects.get(username='MuckrockStaff'),
            to_user=self.get_to_user(),
            date=datetime.now(),
            response=False,
            communication=render_to_string(
                'message/communication/payment.txt',
                {
                    'amount': amount,
                    'payable_to': payable_to,
                    }))
        self.submit(payment=True, snail=True, amount=amount)
        # We perform some logging and activity generation
        logger.info('%s has paid %0.2f for request %s', user.username, amount, self.title)
        utils.new_action(user, 'paid fees', target=self)
        # We return the communication we generated, in case the caller wants to do anything with it
        return comm

    def _send_msg(self, **kwargs):
        """Send a message for this request"""
        # self.email / self.fax / self.address should be set
        # before calling thismethod

        comm = self.communications.last()
        subject = comm.subject or self.default_subject()
        subject = subject[:255]
        comm.subject = subject

        # pylint:disable=attribute-defined-outside-init
        self.reverse_communications = self.communications.reverse()

        # preferred order of communication methods
        if self.email and not kwargs['snail']:
            self._send_email(comm, **kwargs)
        elif self.fax and not kwargs['snail']:
            self._send_fax(comm, **kwargs)
        elif self.address:
            self._send_snail_mail(comm, **kwargs)
        else:
            # XXX shouldnt happen, catch this
            raise ValueError('No where to send to')

        comm.save()

        # unblock incoming messages if we send one out
        self.block_incoming = False
        self.save()

    def get_agency_reply_link(self, email):
        """Get the link for the agency user to log in"""
        agency = self.agency
        agency_user_profile = agency.get_user().profile
        return agency_user_profile.wrap_url(
                reverse(
                    'acct-agency-redirect-login',
                    kwargs={
                        'agency_slug': agency.slug,
                        'agency_idx': agency.pk,
                        'foia_slug': self.slug,
                        'foia_idx': self.pk,
                        },
                    ),
                email=email,
                )

    def _send_email(self, comm, appeal, thanks, show_all_comms):
        """Send the message as an email"""

        from_addr = self.get_mail_id()
        from_email = EmailAddress.objects.get_or_create(
                email='%s@%s' % (from_addr, settings.MAILGUN_SERVER_NAME),
                )

        context = {
                'request': self,
                'show_all_comms': show_all_comms,
                'reply_link': self.get_agency_reply_link(to_emails[0]),
                }
        body = render_to_string(
            'text/foia/request_email.txt',
            context,
            )

        self.status = self._sent_status(appeal, thanks)

        email_comm = EmailCommunication.objects.create(
                communication=comm,
                sent_datetime=datetime.now(),
                from_email=from_email,
                to_emails=to_emails,
                cc_emails=cc_emails,
                )
        msg = EmailMultiAlternatives(
                subject=comm.subject,
                body=body,
                from_email=str(from_email),
                to=str(self.email),
                cc=[str(e) for e in self.cc_emails.all()],
                bcc=['diagnostics@muckrock.com'],
                headers={
                    'X-Mailgun-Variables':
                    {'email_id': email_comm.pk},
                    }
                )
        msg.attach_alternative(linebreaks(escape(body)), 'text/html')
        # atach all files from the latest communication
        comm.attach_files(msg)

        msg.send(fail_silently=False)

        email_comm.set_raw_email(msg.message())

    def _send_fax(self, comm, appeal, thanks, show_all_comms):
        """Send the message as a fax"""
        from muckrock.foia.tasks import send_fax

        context = {
                'request': self,
                'show_all_comms': show_all_comms,
                }
        body = render_to_string(
            'text/foia/request_email.txt',
            context,
            )
        self.status = self._sent_status(appeal, thanks)

        send_fax.apply_async(args=[comm.pk, comm.subject, body])

    def _send_snail_mail(self, comm, **kwargs):
        """Send the message as a snail mail"""
        if not kwargs.get('thanks'):
            self.status = 'submitted'
            self.date_processing = date.today()
        if self.communications.count() == 1:
            category = 'n'
        elif kwargs.get('appeal'):
            category = 'a'
        elif kwargs.get('followup'):
            category = 'f'
        elif kwargs.get('payment'):
            category = 'p'
        else:
            category = 'u'
        if 'amount' in kwargs:
            extra = {'amount': amount}
        else:
            extra = {}
        task.models.SnailMailTask.objects.create(
                category=category,
                communication=comm,
                #address=self.address, # XXX is this needed? if not should we double check the address on the comm once it is sent?
                **extra
                )

    def _sent_status(self, appeal, thanks):
        """After sending out the message, set the correct new status"""
        if thanks:
            return self.status
        elif appeal:
            return 'appealing'
        elif self.has_ack():
            return 'processed'
        else:
            return 'ack'

    def update_dates(self):
        """Set the due date, follow up date and days until due attributes"""
        cal = self.jurisdiction.get_calendar()
        # first submit
        if not self.date_submitted:
            self.date_submitted = date.today()
            days = self.jurisdiction.get_days()
            if days:
                self.date_due = cal.business_days_from(date.today(), days)
        # updated from mailgun without setting status or submitted
        if self.status in ['ack', 'processed']:
            # unpause the count down
            if self.days_until_due is not None:
                self.date_due = cal.business_days_from(date.today(), self.days_until_due)
                self.days_until_due = None
            self._update_followup_date()
        # if we are no longer waiting on the agency, do not follow up
        if self.status not in ['ack', 'processed'] and self.date_followup:
            self.date_followup = None
        # if we need to respond, pause the count down until we do
        if self.status in ['fix', 'payment'] and self.date_due:
            last_datetime = self.last_comm().date
            if not last_datetime:
                last_datetime = datetime.now()
            self.days_until_due = cal.business_days_between(last_datetime.date(), self.date_due)
            self.date_due = None
        self.save()

    def _update_followup_date(self):
        """Update the follow up date"""
        try:
            new_date = self.last_comm().date.date() + timedelta(self._followup_days())
            if self.date_due and self.date_due > new_date:
                new_date = self.date_due

            if not self.date_followup or self.date_followup < new_date:
                self.date_followup = new_date

        except IndexError:
            # This request has no communications at the moment, cannot asign a follow up date
            pass

    def _followup_days(self):
        """How many days do we wait until we follow up?"""
        if self.status == 'ack' and self.jurisdiction:
            # if we have not at least been acknowledged yet, set the days
            # to the period required by law
            jurisdiction_days = self.jurisdiction.get_days()
            if jurisdiction_days is not None:
                return jurisdiction_days
        if self.date_estimate and date.today() < self.date_estimate:
            # return the days until the estimated date
            date_difference = self.date_estimate - date.today()
            return date_difference.days
        if self.jurisdiction and self.jurisdiction.level == 'f':
            return 30
        else:
            return 15

    def update_tags(self, tags):
        """Update the requests tags"""
        tag_set = set()
        for tag in parse_tags(tags):
            new_tag, _ = Tag.objects.get_or_create(name=tag)
            tag_set.add(new_tag)
        self.tags.set(*tag_set)

    def user_actions(self, user):
        '''Provides action interfaces for users'''
        is_owner = self.created_by(user)
        is_agency_user = (user.is_authenticated() and
                user.profile.acct_type == 'agency')
        can_follow = (user.is_authenticated() and not is_owner and
                not is_agency_user)
        is_following = user.is_authenticated() and user in followers(self)
        is_admin = user.is_staff
        kwargs = {
            'jurisdiction': self.jurisdiction.slug,
            'jidx': self.jurisdiction.pk,
            'idx': self.pk,
            'slug': self.slug
        }
        return [
            Action(
                test=not is_agency_user,
                link=reverse('foia-clone', kwargs=kwargs),
                title='Clone',
                desc='Start a new request using this one as a base',
                class_name='primary'
            ),
            Action(
                test=can_follow,
                link=reverse('foia-follow', kwargs=kwargs),
                title=('Unfollow' if is_following else 'Follow'),
                class_name=('default' if is_following else 'primary')
            ),
            Action(
                test=self.has_perm(user, 'flag'),
                title='Get Help',
                action='flag',
                desc=u'Something broken, buggy, or off?  Let us know and we’ll fix it',
                class_name='failure modal'
            ),
            Action(
                test=is_admin,
                title='Contact User',
                action='contact_user',
                desc=u'Send this request\'s owner an email',
                class_name='modal'
            ),
        ]

    def contextual_request_actions(self, user, can_edit):
        '''Provides context-sensitive action interfaces for requests'''
        can_follow_up = can_edit and self.status != 'started'
        can_appeal = self.has_perm(user, 'appeal')
        kwargs = {
            'jurisdiction': self.jurisdiction.slug,
            'jidx': self.jurisdiction.pk,
            'idx': self.pk,
            'slug': self.slug
        }
        return [
            Action(
                test=user.is_staff,
                link=reverse('foia-admin-fix', kwargs=kwargs),
                title='Admin Fix',
                desc='Open the admin fix form',
                class_name='default'
            ),
            Action(
                test=can_edit,
                title='Get Advice',
                action='question',
                desc=u'Get your questions answered by Muckrock’s community of FOIA experts',
                class_name='modal'
            ),
            Action(
                test=can_follow_up,
                title='Follow Up',
                action='follow_up',
                desc='Send a message directly to the agency',
                class_name='reply'
            ),
            Action(
                test=can_appeal,
                title='Appeal',
                action='appeal',
                desc=u'Appeal an agency’s decision',
                class_name='reply'
            ),
        ]

    def total_pages(self):
        """Get the total number of pages for this request"""
        pages = self.files.aggregate(Sum('pages'))['pages__sum']
        if pages is None:
            return 0
        return pages

    def has_ack(self):
        """Has this request been acknowledged?"""
        return self.communications.filter(response=True).exists()

    def proxy_reject(self):
        """Mark this request as being rejected due to a proxy being required"""
        from muckrock.task.models import FlaggedTask
        # mark the agency as requiring a proxy going forward
        self.agency.requires_proxy = True
        self.agency.save()
        # mark to re-file with a proxy
        FlaggedTask.objects.create(
            foia=self,
            text='This request was rejected as requiring a proxy; please refile'
            ' it with one of our volunteers names and a note that the request is'
            ' being filed by a state citizen. Make sure the new request is'
            ' associated with the original user\'s account. To add someone as'
            ' a proxy, change their user type to "Proxy" and make sure they'
            ' properly have their state set on the backend. This message should'
            ' only appear the first time an agency rejects a request for being'
            ' from an out-of-state resident.'
            )
        self.notes.create(
            author=User.objects.get(username='MuckrockStaff'),
            note='The request has been rejected with the agency stating that '
            'you must be a resident of the state. MuckRock is working with our '
            'in-state volunteers to refile this request, and it should appear '
            'in your account within a few days.',
            )

    def default_subject(self):
        """Make a subject line for a communication for this request"""
        law_name = self.jurisdiction.get_law_name()
        if self.tracking_id:
            return 'RE: %s Request #%s' % (law_name, self.tracking_id)
        elif self.communications.count() > 1:
            return 'RE: %s Request: %s' % (law_name, self.title)
        else:
            return '%s Request: %s' % (law_name, self.title)

    class Meta:
        # pylint: disable=too-few-public-methods
        ordering = ['title']
        verbose_name = 'FOIA Request'
        app_label = 'foia'
        permissions = (
            ('view_foiarequest', 'Can view this request'),
            ('embargo_foiarequest', 'Can embargo request to make it private'),
            ('embargo_perm_foiarequest', 'Can embargo a request permananently'),
            ('crowdfund_foiarequest',
                'Can start a crowdfund campaign for the request'),
            ('appeal_foiarequest', 'Can appeal the requests decision'),
            ('thank_foiarequest', 'Can thank the FOI officer for their help'),
            ('flag_foiarequest', 'Can flag the request for staff attention'),
            ('followup_foiarequest', 'Can send a manual follow up'),
            ('agency_reply_foiarequest', 'Can send a direct reply'),
            ('upload_attachment_foiarequest', 'Can upload an attachment'),
            )

