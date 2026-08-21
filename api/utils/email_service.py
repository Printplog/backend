import logging
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.conf import settings
from django.utils import timezone
from api.models import SiteSettings

logger = logging.getLogger(__name__)


class EmailService:
    """
    Every send is queued rather than performed inline.

    SMTP costs well over a second per message — a fresh connection, TLS
    handshake and auth each time — which used to be charged directly to
    whichever request triggered it, login included.
    """

    @staticmethod
    def deliver(subject, template_name, context, recipient_list):
        """
        Actually render and transmit. Called by the Celery worker, or inline as
        a last resort when the broker is unreachable.

        Raises on failure so the task's retry/backoff can do its job.
        """
        site_settings = SiteSettings.get_settings()
        full_context = {
            **context,
            'current_year': timezone.now().year,
            'support_email': site_settings.support_email or settings.DEFAULT_FROM_EMAIL,
            'site_settings': site_settings,
            'frontend_url': settings.FRONTEND_URL,
        }

        html_content = render_to_string(template_name, full_context)
        text_content = strip_tags(html_content)

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=recipient_list,
        )
        msg.attach_alternative(html_content, "text/html")
        msg.send()
        return True

    @staticmethod
    def _send_email(subject, template_name, context, recipient_list):
        """
        Queue an email. Returns whether it was accepted for delivery, not
        whether it arrived — delivery happens later, on the worker.
        """
        recipients = [address for address in recipient_list if address]
        if not recipients:
            logger.warning("Skipping email %r: no recipients", subject)
            return False

        def enqueue():
            from api.tasks import send_email

            try:
                send_email.delay(subject, template_name, context, recipients)
            except Exception:
                # Broker down. Better a slow request than a lost password reset.
                logger.exception("Could not queue email %r; sending inline", subject)
                try:
                    EmailService.deliver(subject, template_name, context, recipients)
                except Exception:
                    logger.exception("Inline send of %r to %s failed", subject, recipients)

        # Outside a transaction this runs immediately; inside one it waits for
        # COMMIT. That is what stops a rolled-back wallet credit from leaving a
        # "wallet funded" email behind it.
        transaction.on_commit(enqueue)
        return True

    @classmethod
    def send_password_reset(cls, email, reset_url):
        subject = "Reset Your Password - SharpToolz"
        context = {'reset_url': reset_url}
        return cls._send_email(subject, 'emails/auth/password_reset.html', context, [email])

    @classmethod
    def send_welcome_email(cls, user):
        subject = "🚀 Welcome to SharpToolz - Your Creative Suite Awaits"
        context = {
            'username': user.username,
            'dashboard_url': f"{settings.FRONTEND_URL}/dashboard"
        }
        return cls._send_email(subject, 'emails/auth/welcome.html', context, [user.email])

    @classmethod
    def send_login_notification(cls, user, ip_address, user_agent):
        subject = "🔒 Security Alert: New Login to your SharpToolz Account"
        context = {
            'username': user.username,
            'ip_address': ip_address,
            'user_agent': user_agent,
            'timestamp': timezone.now().strftime('%b %d, %Y %H:%M:%S %Z'),
            'reset_url': f"{settings.FRONTEND_URL}/auth/forgot-password"
        }
        return cls._send_email(subject, 'emails/auth/login_alert.html', context, [user.email])

    @classmethod
    def send_wallet_funded(cls, user, amount, balance, transaction_id, description):
        subject = f"💰 Wallet Funded: ${amount} Successfully Credited"
        context = {
            'username': user.username,
            'amount': f"{amount:,.2f}",
            'balance': f"{balance:,.2f}",
            'transaction_id': transaction_id,
            'description': description,
            'dashboard_url': f"{settings.FRONTEND_URL}/dashboard/wallet"
        }
        return cls._send_email(subject, 'emails/wallet/funded.html', context, [user.email])

    @classmethod
    def send_payment_notification(cls, user, amount, balance, transaction_id, description):
        subject = f"🧾 Payment Receipt: ${abs(amount)} - SharpToolz"
        context = {
            'username': user.username,
            'amount': f"{abs(amount):,.2f}",
            'balance': f"{balance:,.2f}",
            'transaction_id': transaction_id,
            'description': description,
            'dashboard_url': f"{settings.FRONTEND_URL}/dashboard/wallet"
        }
        return cls._send_email(subject, 'emails/wallet/payment.html', context, [user.email])

    @classmethod
    def send_purchase_receipt(cls, user, template_name, amount, balance, transaction_id):
        subject = f"🎨 Order Confirmed: {template_name}"
        context = {
            'username': user.username,
            'template_name': template_name,
            'amount': f"{amount:,.2f}",
            'balance': f"{balance:,.2f}",
            'transaction_id': transaction_id,
            'dashboard_url': f"{settings.FRONTEND_URL}/dashboard/documents"
        }
        return cls._send_email(subject, 'emails/wallet/purchase_receipt.html', context, [user.email])

    @classmethod
    def send_referral_reminder(cls, friend_email, friend_name, referrer_name):
        subject = f"🎁 {referrer_name} sent you a 10% Cash Bonus!"
        context = {
            'friend_name': friend_name,
            'referrer_name': referrer_name,
            'deposit_url': f"{settings.FRONTEND_URL}/dashboard/wallet"
        }
        return cls._send_email(subject, 'emails/referral/reminder.html', context, [friend_email])

    @classmethod
    def send_contact_form(cls, name, email, subject, message):
        site_settings = SiteSettings.get_settings()
        dest_email = site_settings.support_email or "support@sharptoolz.com"
        
        email_subject = f"NEW CONTACT MESSAGE: {subject}"
        context = {
            'sender_name': name,
            'sender_email': email,
            'msg_subject': subject,
            'message': message,
        }
        # We'll use a generic contact notification template
        return cls._send_email(email_subject, 'emails/site/contact_notification.html', context, [dest_email])
