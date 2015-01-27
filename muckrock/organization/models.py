"""
Models for the organization application
"""

from django.contrib.auth.models import User
from django.core.mail import EmailMessage
from django.db import models
from django.template.loader import render_to_string

from muckrock.settings import MONTHLY_REQUESTS

from datetime import datetime
import logging
import stripe

logger = logging.getLogger(__name__)

class Organization(models.Model):
    """Orginization to allow pooled requests and collaboration"""

    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(max_length=255, unique=True)
    owner = models.ForeignKey(User)
    num_requests = models.IntegerField(default=0)
    date_update = models.DateField()
    stripe_id = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=False)

    def __unicode__(self):
        return self.name

    @models.permalink
    def get_absolute_url(self):
        """The url for this object"""
        return ('org-detail', [], {'slug': self.slug})

    def get_requests(self):
        """Get the number of requests left for this month"""
        not_this_month = self.date_update.month != datetime.now().month
        not_this_year = self.date_update.year != datetime.now().year
        if not_this_month or not_this_year and self.active:
            # update requests if they have not yet been updated this month
            self.date_update = datetime.now()
            self.num_requests = MONTHLY_REQUESTS.get('org', 0)
            self.save()
        return self.num_requests

    def get_members(self):
        """Get a queryset of profiles who are members of the organization"""
        # TODO: fix scope bug
        # Profile is imported here because Python says it can't find
        # muckrock.accounts.models.Profile when the import statement
        # is scoped at the file or class level? A bug to look into.
        from muckrock.accounts.models import Profile
        return Profile.objects.filter(organization=self)

    def is_owned_by(self, user):
        """Answers whether the passed user owns the org"""
        return self.owner == user
    
    def is_active(self):
        return self.active

    def add_member(self, user):
        """Add a user to this organization"""
        profile = user.get_profile()
        if not profile.is_member_of(self): # doesn't update if already a member
            profile.organization = self
            profile.save()
            # send an email notifying the user
            msg = render_to_string('text/organization/add_member.txt', {
                'member_name': user.first_name,
                'organization_name': self.name,
                'organization_owner': self.owner.get_full_name(),
                'organization_link': self.get_absolute_url()
            })
            email = EmailMessage(
                subject='[MuckRock] You were added to an organization',
                body=msg,
                from_email='info@muckrock.com',
                to=[user.email],
                bcc=['diagnostics@muckrock.com']
            )
            email.send(fail_silently=False)
            logger.info('%s was added as a member of the %s organization', user.username, self.name)
        return

    def remove_member(self, user):
        """Remove a user (who isn't the owner) from this organization"""
        if not self.is_owned_by(user):      
            profile = user.get_profile()
            profile.organization = None
            profile.save()
            # send an email notifying the user
            msg = render_to_string('text/organization/remove_member.txt', {
                'member_name': user.first_name,
                'organization_name': self.name,
                'organization_owner': self.owner.get_full_name(),
            })
            email = EmailMessage(
                subject='[MuckRock] You were removed from an organization',
                body=msg,
                from_email='info@muckrock.com',
                to=[user.email],
                bcc=['diagnostics@muckrock.com']
            )
            email.send(fail_silently=False)
            logger.info('%s was removed as a member of the %s organization', user.username, self.name)
        return

    def start_subscription(self):
        """Create an org subscription for the owner"""
        customer = stripe.Customer.retrieve(self.stripe_id)
        profile = self.owner.get_profile()
        if profile.acct_type == 'pro':
            profile.acct_type = 'community'
            profile.save()
        customer.update_subscription(plan='org')
        customer.save()
        self.active = True
        self.save()
        return

    def pause_subscription(self):
        """Cancel the org's subscription"""
        customer = stripe.Customer.retrieve(self.stripe_id)
        customer.cancel_subscription()
        customer.save()
        self.active = False
        self.save()
        return