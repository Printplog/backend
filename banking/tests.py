from datetime import timedelta
from decimal import Decimal
import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from wallet.models import Wallet
from .models import FernwayProfile

User = get_user_model()


class FernwayMultiAccountSubscriptionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='banktester',
            email='owner@sharptoolz.test',
            password='strongpassword123',
        )
        self.wallet, _ = Wallet.objects.get_or_create(user=self.user)
        self.wallet.balance = Decimal('50.00')
        self.wallet.save()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _verify_email(self, client: APIClient, email: str, profile_id: str | None = None) -> str:
        sent_payloads = []

        def fake_send(**kwargs):
            sent_payloads.append(kwargs)
            return 'resend-verify-id'

        body = {'email': email}
        if profile_id:
            body['profile_id'] = profile_id

        with patch('banking.notifications._send_resend_email', side_effect=fake_send):
            req_res = client.post('/api/fernway/email-verification/request/', body, format='json')
        self.assertEqual(req_res.status_code, 200, req_res.content)
        challenge_id = req_res.json()['challenge_id']
        self.assertEqual(len(sent_payloads), 1)
        match = re.search(r'\b(\d{4})\b', sent_payloads[0]['subject'])
        self.assertIsNotNone(match)
        code = match.group(1)

        confirm_res = client.post(
            '/api/fernway/email-verification/confirm/',
            {'challenge_id': challenge_id, 'code': code},
            format='json',
        )
        self.assertEqual(confirm_res.status_code, 200, confirm_res.content)
        return confirm_res.json()['verification_token']

    def test_user_can_create_multiple_accounts_with_card_details_and_subscriptions(self):
        # 0. Creating without verifying email is rejected
        unverified = self.client.post('/api/fernway/accounts/', {
            'test': True,
            'email': 'ada@fernwaybank.com',
            'password': 'secret123',
        }, format='json')
        self.assertEqual(unverified.status_code, 400)

        # 1. Verify email & create a free Test Account
        ada_token = self._verify_email(self.client, 'ada@fernwaybank.com')
        res1 = self.client.post('/api/fernway/accounts/', {
            'test': True,
            'first_name': 'Ada',
            'last_name': 'Lovelace',
            'email': 'ada@fernwaybank.com',
            'email_verification_token': ada_token,
            'password': 'secret123',
            'transaction_pin': '4821',
            'account_balance': '350000.00',
            'card_holder_name': 'Ada Lovelace',
            'card_number': '4532 1111 2222 3333',
            'card_expiry': '09/29',
            'card_cvv': '904',
            'card_pin': '7391',
            'card_limit': '25000.00',
        }, format='json')
        self.assertEqual(res1.status_code, 201)
        data1 = res1.json()
        self.assertTrue(data1['email_verified'])
        self.assertFalse(data1['is_live'])
        self.assertEqual(data1['subscription_status'], 'test')
        self.assertEqual(len(data1['accounts']), 1)
        self.assertEqual(data1['accounts'][0]['balance'], 35000000)
        self.assertEqual(data1['cards'][0]['card_number'], '4532 1111 2222 3333')
        self.assertEqual(data1['cards'][0]['last4'], '3333')
        self.assertEqual(data1['cards'][0]['expiry'], '09/29')
        self.assertEqual(data1['cards'][0]['cvv'], '904')
        self.assertEqual(data1['cards'][0]['pin'], '7391')
        self.assertEqual(data1['cards'][0]['kind'], 'physical')
        self.assertEqual(data1['transaction_pin'], '4821')

        # 2. Verify email & create a second Live Monthly Account (paid from wallet)
        grace_token = self._verify_email(self.client, 'grace@fernwaybank.com')
        res2 = self.client.post('/api/fernway/accounts/', {
            'test': False,
            'first_name': 'Grace',
            'last_name': 'Hopper',
            'email': 'grace@fernwaybank.com',
            'email_verification_token': grace_token,
            'password': 'secret456',
            'account_balance': '1000000.00',
            'card_number': '4532 4444 5555 6666',
            'card_expiry': '11/30',
            'card_cvv': '512',
        }, format='json')
        self.assertEqual(res2.status_code, 201)
        data2 = res2.json()
        self.assertTrue(data2['is_live'])
        self.assertEqual(data2['subscription_status'], 'active')
        self.assertEqual(data2['days_remaining'], 30)

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('35.00'))

        # 3. Duplicate sign-in email is rejected
        dup = self.client.post('/api/fernway/accounts/', {
            'test': True,
            'email': 'grace@fernwaybank.com',
            'email_verification_token': grace_token,
            'password': 'anotherpassword',
        }, format='json')
        self.assertEqual(dup.status_code, 400)

        # 4. List returns both accounts & subscription counts
        list_res = self.client.get('/api/fernway/accounts/')
        self.assertEqual(list_res.status_code, 200)
        listing = list_res.json()
        self.assertEqual(listing['count'], 2)
        self.assertEqual(listing['active_subscriptions_count'], 1)

        # 5. Renewing an expired subscription extends by 30 days & restores live status
        profile2 = FernwayProfile.objects.get(id=data2['id'])
        profile2.subscription_expires_at = timezone.now() - timedelta(days=1)
        profile2.save()
        self.assertFalse(profile2.is_live)
        self.assertEqual(profile2.effective_subscription_status, 'expired')

        renew_res = self.client.post(f"/api/fernway/accounts/{profile2.id}/renew/")
        self.assertEqual(renew_res.status_code, 200)
        renewed = renew_res.json()
        self.assertTrue(renewed['is_live'])
        self.assertEqual(renewed['subscription_status'], 'active')
        self.assertEqual(renewed['days_remaining'], 30)

        # 6. Fernway client login & data lookup isolate accounts cleanly
        anon = APIClient()
        login_res = anon.post('/api/fernway/client/auth/login/', {
            'email': 'ada@fernwaybank.com',
            'password': 'secret123',
        }, format='json')
        self.assertEqual(login_res.status_code, 200)
        token = login_res.json()['token']

        client_data = anon.get(f'/api/fernway/client/data/?token={token}')
        self.assertEqual(client_data.status_code, 200)
        self.assertEqual(client_data.json()['email'], 'ada@fernwaybank.com')
        self.assertFalse(client_data.json()['is_live'])

        # 7. Wrong transaction PIN is rejected; valid PIN creates pending outgoing transfer
        bad_pin = anon.post('/api/fernway/client/transfers/', {
            'recipient_name': 'Jane Receiver',
            'bank_name': 'Chase Bank',
            'account_number': '9988776655',
            'amount_cents': 500000,
            'transaction_pin': '0000',
        }, format='json', HTTP_X_FERNWAY_TOKEN=token)
        self.assertEqual(bad_pin.status_code, 400)

        tx_res = anon.post('/api/fernway/client/transfers/', {
            'recipient_name': 'Jane Receiver',
            'bank_name': 'Chase Bank',
            'account_number': '9988776655',
            'routing_number': '021000021',
            'amount_cents': 500000,  # $5,000.00
            'note': 'Invoice #42',
            'transaction_pin': '4821',
        }, format='json', HTTP_X_FERNWAY_TOKEN=token)
        self.assertEqual(tx_res.status_code, 201)
        pending_tx = tx_res.json()
        self.assertEqual(pending_tx['status'], 'pending')
        self.assertEqual(pending_tx['bank_name'], 'Chase Bank')
        self.assertEqual(pending_tx['recipient_account_number'], '9988776655')

        # SharpToolz owner sees 1 pending transfer on account detail & approves it
        detail = self.client.get(f"/api/fernway/accounts/{data1['id']}/").json()
        self.assertEqual(detail['pending_transfers_count'], 1)
        self.assertEqual(detail['accounts'][0]['balance'], 34500000)

        approve_res = self.client.post(
            f"/api/fernway/accounts/{data1['id']}/transaction/{pending_tx['id']}/approve/"
        )
        self.assertEqual(approve_res.status_code, 200)
        approved_profile = approve_res.json()
        self.assertEqual(approved_profile['pending_transfers_count'], 0)
        self.assertEqual(approved_profile['accounts'][0]['balance'], 34500000)

        # 8. Another outgoing transfer declined -> marks failed & refunds balance
        tx_res2 = anon.post('/api/fernway/client/transfers/', {
            'recipient_name': 'Bob Refund',
            'bank_name': 'Wells Fargo',
            'account_number': '1122334455',
            'amount_cents': 100000,  # $1,000.00
            'transaction_pin': '4821',
        }, format='json', HTTP_X_FERNWAY_TOKEN=token)
        self.assertEqual(tx_res2.status_code, 201)
        tx2_id = tx_res2.json()['id']

        decline_res = self.client.post(
            f"/api/fernway/accounts/{data1['id']}/transaction/{tx2_id}/decline/"
        )
        self.assertEqual(decline_res.status_code, 200)
        self.assertEqual(decline_res.json()['accounts'][0]['balance'], 34500000)

    def test_dual_branded_notifications_and_card_sync_via_resend(self):
        verified_email = 'customer@realinbox.test'
        verify_token = self._verify_email(self.client, verified_email)
        created = self.client.post('/api/fernway/accounts/', {
            'test': True,
            'first_name': 'Nora',
            'last_name': 'Vance',
            'email': verified_email,
            'email_verification_token': verify_token,
            'password': 'password123',
            'transaction_pin': '2468',
        }, format='json').json()
        pid = created['id']
        fw_token = created['auth_token']

        # Changing email without re-verifying is rejected
        bad_email_change = self.client.put(
            f'/api/fernway/accounts/{pid}/',
            {'email': 'newemail@realinbox.test'},
            format='json',
        )
        self.assertEqual(bad_email_change.status_code, 400)

        sent = []

        def capture_send(**kwargs):
            sent.append(kwargs)
            return f"email-{len(sent)}"

        with (
            patch('banking.notifications.get_integration_secret', return_value='re_test_key'),
            patch('banking.notifications._send_resend_email', side_effect=capture_send),
        ):
            # 1. Fernway login notifies BOTH SharpToolz owner AND verified Fernway account email
            anon = APIClient()
            login_res = anon.post(
                '/api/fernway/client/auth/login/',
                {'email': verified_email, 'password': 'password123', 'client_ip': '198.51.100.5'},
                format='json',
            )
            self.assertEqual(login_res.status_code, 200)
            self.assertEqual(len(sent), 2)
            recipients = {item['recipient']: item for item in sent}
            self.assertIn('owner@sharptoolz.test', recipients)
            self.assertIn(verified_email, recipients)
            self.assertIn('security@fernwaybank.com', recipients[verified_email]['from_email'])
            self.assertIn('SharpToolz', recipients['owner@sharptoolz.test']['html'])
            self.assertIn('fernwaybank.com', recipients[verified_email]['html'])

            # 2. Customer adds a physical card from Fernway -> synced in Django & notifies both
            sent.clear()
            card_res = anon.post(
                '/api/fernway/client/cards/',
                {'label': 'Travel Debit', 'monthlyLimit': 500000},
                format='json',
                HTTP_X_FERNWAY_TOKEN=fw_token,
            )
            self.assertEqual(card_res.status_code, 201)
            new_card = card_res.json()
            self.assertEqual(new_card['label'], 'Travel Debit')
            self.assertEqual(len(sent), 2)

            # 3. Customer freezes the card -> notifies both
            sent.clear()
            patch_res = anon.patch(
                f"/api/fernway/client/cards/{new_card['id']}/",
                {'frozen': True},
                format='json',
                HTTP_X_FERNWAY_TOKEN=fw_token,
            )
            self.assertEqual(patch_res.status_code, 200)
            self.assertTrue(patch_res.json()['is_frozen'])
            self.assertEqual(len(sent), 2)

            # 4. Customer submits withdrawal/transfer -> notifies both; owner approves -> notifies Fernway email
            sent.clear()
            tx_res = anon.post(
                '/api/fernway/client/transfers/',
                {
                    'recipient_name': 'Marcus Vance',
                    'bank_name': 'Chase',
                    'account_number': '1234567890',
                    'amount_cents': 250000,
                    'transaction_pin': '2468',
                },
                format='json',
                HTTP_X_FERNWAY_TOKEN=fw_token,
            )
            self.assertEqual(tx_res.status_code, 201)
            self.assertEqual(len(sent), 2)

            sent.clear()
            approve_res = self.client.post(
                f"/api/fernway/accounts/{pid}/transaction/{tx_res.json()['id']}/approve/"
            )
            self.assertEqual(approve_res.status_code, 200)
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]['recipient'], verified_email)
            self.assertIn('Completed', sent[0]['subject'])

            # 5. Owner posts a deposit transaction -> notifies verified Fernway email
            sent.clear()
            deposit_res = self.client.post(
                f'/api/fernway/accounts/{pid}/transaction/',
                {
                    'direction': 'credit',
                    'amount': 1250.00,
                    'counterparty': 'Payroll Deposit',
                    'narration': 'Bonus',
                },
                format='json',
            )
            self.assertEqual(deposit_res.status_code, 201)
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]['recipient'], verified_email)
            self.assertIn('Deposit Received', sent[0]['subject'])

    def test_fernway_tool_seeded_and_price_updates_dynamically(self):
        from api.models import Tool
        tool = Tool.objects.filter(slug='fernway').first()
        self.assertIsNotNone(tool)
        self.assertEqual(tool.price, Decimal('15.00'))

        # Admin price change immediately reflects in monthly_price
        tool.price = Decimal('25.00')
        tool.save(update_fields=['price'])
        meta = self.client.get('/api/fernway/manage/').json()
        self.assertEqual(meta['monthly_price'], 25.0)

    def test_admin_can_list_edit_toggle_subscription_and_approve_any_users_account(self):
        # Standard user creates a Test account with verified email
        owned_token = self._verify_email(self.client, 'owned@fernwaybank.com')
        created = self.client.post('/api/fernway/accounts/', {
            'test': True,
            'first_name': 'User',
            'last_name': 'Owner',
            'email': 'owned@fernwaybank.com',
            'email_verification_token': owned_token,
            'password': 'secret123',
            'transaction_pin': '2468',
            'account_balance': '100000.00',
        }, format='json').json()
        pid = created['id']

        # Non-admin user is forbidden from admin endpoints
        self.assertEqual(self.client.get('/api/fernway/admin/accounts/').status_code, 403)

        # Create staff admin
        admin_user = User.objects.create_user(
            username='superadmin',
            email='admin@sharptoolz.test',
            password='adminpassword123',
            is_staff=True,
            is_superuser=True,
        )
        admin_client = APIClient()
        admin_client.force_authenticate(user=admin_user)

        # Admin lists all accounts across users
        listing = admin_client.get('/api/fernway/admin/accounts/').json()
        self.assertEqual(listing['count'], 1)
        self.assertEqual(listing['results'][0]['owner']['username'], 'banktester')

        # Admin activates live mode for 30 days without debiting owner's wallet
        sub_res = admin_client.post(
            f'/api/fernway/admin/accounts/{pid}/subscription/',
            {'action': 'activate', 'days': 30},
            format='json',
        )
        self.assertEqual(sub_res.status_code, 200)
        self.assertTrue(sub_res.json()['is_live'])

        # Admin edits balance and transaction PIN on owner's account
        upd_res = admin_client.put(
            f'/api/fernway/admin/accounts/{pid}/',
            {'account_balance': '500000.00', 'transaction_pin': '9999'},
            format='json',
        )
        self.assertEqual(upd_res.status_code, 200)
        self.assertEqual(upd_res.json()['accounts'][0]['balance'], 50000000)
        self.assertEqual(upd_res.json()['transaction_pin'], '9999')

        # Fernway transfer with new PIN -> Admin approves it
        anon = APIClient()
        tx = anon.post('/api/fernway/client/transfers/', {
            'recipient_name': 'Recipient One',
            'bank_name': 'Citi',
            'account_number': '5544332211',
            'amount_cents': 250000,
            'transaction_pin': '9999',
        }, format='json', HTTP_X_FERNWAY_TOKEN=created['auth_token']).json()

        approve = admin_client.post(f"/api/fernway/admin/accounts/{pid}/transaction/{tx['id']}/approve/")
        self.assertEqual(approve.status_code, 200)
        self.assertEqual(approve.json()['pending_transfers_count'], 0)

    def test_fernway_live_support_end_to_end_and_isolation(self):
        # Owner creates a verified account; another user creates a second
        owner_client = APIClient()
        owner_client.force_authenticate(user=self.user)
        grace_token = self._verify_email(owner_client, 'grace-support@fernwaybank.com')
        acct_res = owner_client.post('/api/fernway/accounts/', {
            'test': True,
            'first_name': 'Grace',
            'last_name': 'Hopper',
            'email': 'grace-support@fernwaybank.com',
            'email_verification_token': grace_token,
            'password': 'password123',
        }, format='json')
        self.assertEqual(acct_res.status_code, 201)
        acct = acct_res.json()

        other_user = User.objects.create_user(username='other', email='other@example.com', password='pw')
        other_client = APIClient()
        other_client.force_authenticate(user=other_user)
        alan_token = self._verify_email(other_client, 'alan@fernwaybank.com')
        other_acct = other_client.post('/api/fernway/accounts/', {
            'test': True,
            'first_name': 'Alan',
            'last_name': 'Turing',
            'email': 'alan@fernwaybank.com',
            'email_verification_token': alan_token,
            'password': 'password123',
        }, format='json').json()

        # Fernway customer starts a live support conversation using X-Fernway-Token
        anon = APIClient()
        create_res = anon.post(
            '/api/fernway/client/support/',
            {'subject': 'Transfer status', 'message': 'When will my wire clear?'},
            format='json',
            HTTP_X_FERNWAY_TOKEN=acct['auth_token'],
        )
        self.assertEqual(create_res.status_code, 201)
        thread = create_res.json()
        self.assertEqual(thread['subject'], 'Transfer status')
        self.assertEqual(len(thread['conversation']), 1)

        # SharpToolz owner sees it in unified /api/support/messages/ AND filtered by fernway_profile_id
        inbox_res = owner_client.get('/api/support/messages/')
        self.assertEqual(inbox_res.status_code, 200)
        self.assertEqual(inbox_res.json()['unread_count'], 1)
        self.assertEqual(inbox_res.json()['results'][0]['source'], 'fernway')
        self.assertEqual(inbox_res.json()['results'][0]['fernway_profile_id'], acct['id'])
        # Verified Fernway account populates customer_email so email replies work too
        self.assertEqual(inbox_res.json()['results'][0]['customer_email'], 'grace-support@fernwaybank.com')

        filtered_res = owner_client.get(f"/api/support/messages/?fernway_profile_id={acct['id']}")
        self.assertEqual(len(filtered_res.json()['results']), 1)

        # Other user cannot see or reply to owner's Fernway support thread
        other_inbox = other_client.get('/api/support/messages/')
        self.assertEqual(len(other_inbox.json()['results']), 0)
        other_fernway_list = anon.get(
            '/api/fernway/client/support/',
            HTTP_X_FERNWAY_TOKEN=other_acct['auth_token'],
        )
        self.assertEqual(len(other_fernway_list.json()['results']), 0)

        # SharpToolz owner replies live (mock Resend send since customer_email is verified)
        with patch('api.views.support.send_owner_reply', return_value='resend-reply-id'):
            reply_res = owner_client.post(
                f"/api/support/messages/{thread['id']}/replies/",
                {'body': 'Your transfer has been approved and is on the way.'},
                format='json',
            )
        self.assertEqual(reply_res.status_code, 201)

        # Customer sees owner reply and sends follow-up
        detail_res = anon.get(
            f"/api/fernway/client/support/{thread['id']}/",
            HTTP_X_FERNWAY_TOKEN=acct['auth_token'],
        )
        self.assertEqual(detail_res.status_code, 200)
        self.assertEqual(len(detail_res.json()['conversation']), 2)
        self.assertEqual(detail_res.json()['conversation'][1]['direction'], 'owner')

        cust_reply = anon.post(
            f"/api/fernway/client/support/{thread['id']}/replies/",
            {'body': 'Thank you!'},
            format='json',
            HTTP_X_FERNWAY_TOKEN=acct['auth_token'],
        )
        self.assertEqual(cust_reply.status_code, 201)

        final_inbox = owner_client.get('/api/support/messages/')
        self.assertEqual(final_inbox.json()['unread_count'], 1)
        self.assertEqual(len(final_inbox.json()['results'][0]['conversation']), 3)
