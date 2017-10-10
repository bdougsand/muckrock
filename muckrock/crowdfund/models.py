"""
Models for the crowdfund application
"""

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.urlresolvers import reverse
from django.db import models
from django.db.models import Q

from datetime import date
from decimal import Decimal
import logging
import stripe

from muckrock import task
from muckrock.message.email import TemplateEmail
from muckrock.utils import new_action, stripe_retry_on_error

stripe.api_version = '2015-10-16'


class Crowdfund(models.Model):
    """Crowdfunding campaign"""
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    payment_capped = models.BooleanField(default=False)
    payment_required = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default='0.00'
    )
    payment_received = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default='0.00'
    )
    date_due = models.DateField(blank=True, null=True)
    closed = models.BooleanField(default=False)

    def __unicode__(self):
        return self.name

    def get_absolute_url(self):
        """The url for this object"""
        return reverse('crowdfund', kwargs={'pk': self.pk})

    def expired(self):
        """Has this crowdfund run out of time?"""
        return ((self.date_due is not None and date.today() >= self.date_due)
                or self.closed)

    def amount_remaining(self):
        """Reports the amount still needed to be raised as a decimal."""
        return Decimal(self.payment_required) - Decimal(self.payment_received)

    def percent_funded(self):
        """Reports the percent of the amount required that has been funded."""
        return int(self.payment_received/self.payment_required * 100)

    def update_payment_received(self):
        """Combine the amounts of all the payments"""
        total_amount = Decimal()
        payments = self.payments.all()
        for payment in payments:
            logging.debug(payment)
            total_amount += payment.amount
        self.payment_received = total_amount
        self.save()
        if self.payment_received >= self.payment_required and self.payment_capped:
            self.close_crowdfund(succeeded=True)
        return

    def close_crowdfund(self, succeeded=False):
        """Close the crowdfund and create a new task for it once it reaches its goal."""
        self.closed = True
        self.save()
        task.models.CrowdfundTask.objects.create(crowdfund=self)
        verb = 'ended'
        if succeeded:
            logging.info('Crowdfund %d reached its goal.', self.id)
            verb = 'succeeded'
        new_action(self, verb)
        return

    def contributors_count(self):
        """Return a count of all the contributors to a crowdfund"""
        return self.payments.count()

    def anonymous_contributors_count(self):
        """Return a count of anonymous contributors"""
        return self.payments.filter(Q(show=False) | Q(user=None)).count()

    def named_contributors(self):
        """Return unique named contributors only."""
        # returns the list of a set of a list to remove duplicates
        return User.objects.filter(
                crowdfundpayment__crowdfund=self,
                crowdfundpayment__show=True).distinct()

    def get_crowdfund_object(self):
        """Is this for a request or a project?"""
        if hasattr(self, 'foia'):
            return self.foia
        elif self.project:
            return self.project
        else:
            raise ValueError('Exactly one of foia or project should be set')

    def make_payment(self, token, email, amount, show=False, user=None):
        """Creates a payment for the crowdfund"""
        # pylint: disable=too-many-arguments
        amount = Decimal(amount)
        if self.payment_capped and amount > self.amount_remaining():
            amount = self.amount_remaining()
        # Try processing the payment using Stripe.
        # If the payment fails, do not catch the error.
        # Stripe represents currency as smallest-unit integers.
        stripe_amount = int(float(amount) * 100)
        charge = stripe_retry_on_error(
                stripe.Charge.create,
                amount=stripe_amount,
                source=token,
                currency='usd',
                metadata={
                    'email': email,
                    'action': 'crowdfund-payment',
                    'crowdfund_id': self.id,
                    'crowdfund_name': self.name
                    },
                idempotency_key=True,
                )
        payment = CrowdfundPayment.objects.create(
            amount=amount,
            crowdfund=self,
            user=user,
            show=show,
            charge_id=charge.id
        )
        cache.delete('cf:%s:crowdfund_widget_data' % self.pk)
        logging.info(payment)
        self.update_payment_received()
        return payment

    def send_intro_email(self, user):
        """Send an intro email to the user upon crowdfund creation"""
        msg = TemplateEmail(
                subject='Crowdfund Campaign Launched',
                from_email='info@muckrock.com',
                to=[user.email],
                bcc=['diagnostics@muckrock', 'info@muckrock'],
                text_template='crowdfund/email/intro.txt',
                html_template='crowdfund/email/intro.html',
                extra_context={
                    'user': user.first_name,
                    'amount': self.payment_required,
                    'url': self.get_crowdfund_object().get_absolute_url(),
                    }
                )
        msg.send(fail_silently=False)

    @property
    def project(self):
        """Get the project for this crowdfund if it exists"""
        # there will never be more than one project due to unique constraint
        # pylint: disable=access-member-before-definition
        # pylint: disable=attribute-defined-outside-init
        if hasattr(self, '_project'):
            return self._project
        projects = self.projects.all()
        if projects:
            self._project = projects[0]
        else:
            self._project = None
        return self._project


class CrowdfundPayment(models.Model):
    """A payment toward a crowdfund campaign"""
    user = models.ForeignKey(User, blank=True, null=True)
    name = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    date = models.DateTimeField(auto_now_add=True)
    show = models.BooleanField(default=False)
    charge_id = models.CharField(max_length=255, blank=True)
    crowdfund = models.ForeignKey(Crowdfund, related_name='payments')

    def __unicode__(self):
        return (u'Payment of $%.2f by %s on %s for %s' %
            (self.amount, self.user, self.date.date(),
                self.crowdfund.get_crowdfund_object()))
