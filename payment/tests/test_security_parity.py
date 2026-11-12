"""
Security parity of payments: rows and writes stay within the premiums (policies) the user
may see. PaymentDetail.row_scope = ParentScope("premium"); a Payment is visible when one
of its current details is; a payment with no matched detail only to a user who is not
location restricted (IMIS admin).

Fixture: core.test_helpers.create_security_tree -> R1>D1>W1>V1 / R2>D2>W2>V2; a policy
with a premium and a payment matched to it in each village, plus an unmatched payment;
`user_a` holds the payment rights globally and is assigned district D1 only.
"""
import datetime
import json

from django.core.cache import cache
from django.test import TestCase, override_settings

from contribution.test_helpers import create_test_premium
from core.models.openimis_graphql_test_case import openIMISGraphQLTestCase
from core.test_helpers import (create_security_tree, create_right_only_user, create_test_interactive_user,
                               get_test_token, perm_ids)
from insuree.test_helpers import create_test_insuree
from payment.apps import PaymentConfig
from payment.models import Payment, PaymentDetail
from policy.apps import PolicyConfig
from policy.models import Policy
from policy.test_helpers import create_test_policy


def _u(*lists):
    return list(dict.fromkeys(r for lst in lists for r in lst))


def _product(code):
    """product.test_helpers imports program.test_helpers, absent on Cameroon's program."""
    from program.models import Program
    from product.models import Product
    program = Program.objects.create(code=code[:10], nameProgram=f"Program {code}")
    return Product.objects.create(
        code=code, name=f"Test product {code}", lump_sum=100, max_members=5,
        grace_period_enrolment=1, insurance_period=12, date_from="2019-01-01", date_to="2049-01-01",
        validity_from="2019-01-01", audit_user_id=-1, program=program)


PAYMENT_RIGHTS = _u(perm_ids(PaymentConfig.gql_query_payments_perms),
                    perm_ids(PaymentConfig.gql_mutation_create_payments_perms),
                    perm_ids(PaymentConfig.gql_mutation_update_payments_perms),
                    perm_ids(PaymentConfig.gql_mutation_delete_payments_perms))
POLICY_RIGHTS = _u(perm_ids(PolicyConfig.gql_query_policies_perms),
                   perm_ids(PolicyConfig.gql_mutation_delete_policies_perms))


def _payment(receipt, premium=None):
    payment = Payment.objects.create(
        received_amount=1000, request_date="2020-01-01", received_date="2020-01-03",
        status=Payment.STATUS_UNMATCHED, receipt_no=receipt)
    if premium is not None:
        PaymentDetail.objects.create(
            payment=payment, premium=premium, audit_user_id=-1, validity_from="2019-01-01",
            policy_stage="N", amount=1000, insurance_number=f"{receipt}-chf")
    return payment


def _build(cls, prefix):
    cls.t = create_security_tree(prefix)
    cls.product = _product(f"{prefix}PR")
    for key, village in (("a", cls.t.v1), ("b", cls.t.v2)):
        insuree = create_test_insuree(
            with_family=True, is_head=True,
            custom_props={"chf_id": f"{prefix}0000{1 if key == 'a' else 2}", "current_village": village},
            family_custom_props={"location": village})
        policy = create_test_policy(cls.product, insuree, link=True)
        premium = create_test_premium(policy.id, with_payer=False, custom_props={"receipt": f"{prefix}-{key}"})
        setattr(cls, f"pol_{key}", policy)
        setattr(cls, f"prem_{key}", premium)
        setattr(cls, f"pay_{key}", _payment(f"{prefix}-{key}", premium))
    cls.pay_none = _payment(f"{prefix}-none")
    cls.user_a = create_right_only_user(f"{prefix.lower()}user", _u(PAYMENT_RIGHTS, POLICY_RIGHTS), [cls.t.d1.id])
    cls.admin = create_test_interactive_user(username=f"{prefix.lower()}admin")


def _messages(result):
    return json.dumps(result or [], default=str).lower()


@override_settings(ROW_SECURITY=True)
class PaymentReadScopeTest(openIMISGraphQLTestCase):

    @classmethod
    def setUpTestData(cls):
        _build(cls, "PMR")
        cls.token = get_test_token(cls.user_a)
        cls.admin_token = get_test_token(cls.admin)

    def setUp(self):
        cache.clear()

    def _q(self, query, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return json.loads(self.query(query, headers=headers).content)

    def _receipts(self, query, field, token):
        content = self._q(query, token)
        self.assertNotIn("errors", content, content.get("errors"))
        return {e["node"]["receiptNo"] for e in content["data"][field]["edges"]}

    def test_model_scope(self):
        ids = set(Payment.get_queryset(Payment.objects.all(), self.user_a).values_list("id", flat=True))
        self.assertIn(self.pay_a.id, ids)
        self.assertNotIn(self.pay_b.id, ids)
        self.assertNotIn(self.pay_none.id, ids)
        details = PaymentDetail.get_queryset(PaymentDetail.objects.all(), self.user_a)
        self.assertEqual({self.pay_a.id}, set(details.filter(
            payment__in=[self.pay_a, self.pay_b]).values_list("payment_id", flat=True)))

    def test_anonymous_sees_nothing(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertFalse(Payment.get_queryset(Payment.objects.all(), AnonymousUser()).exists())
        self.assertFalse(PaymentDetail.get_queryset(PaymentDetail.objects.all(), None).exists())

    def test_payments_listed_within_the_districts(self):
        receipts = self._receipts('{ payments { edges { node { receiptNo } } } }', "payments", self.token)
        self.assertIn("PMR-a", receipts)
        self.assertNotIn("PMR-b", receipts)
        self.assertNotIn("PMR-none", receipts)

    def test_unmatched_payment_visible_to_an_admin(self):
        receipts = self._receipts('{ payments { edges { node { receiptNo } } } }', "payments", self.admin_token)
        self.assertTrue({"PMR-a", "PMR-b", "PMR-none"} <= receipts, receipts)

    def test_payment_details_scoped(self):
        """it returned the whole PaymentDetail table (resolver body `pass`)"""
        content = self._q('{ paymentDetails { edges { node { insuranceNumber } } } }', self.token)
        self.assertNotIn("errors", content, content.get("errors"))
        numbers = {e["node"]["insuranceNumber"] for e in content["data"]["paymentDetails"]["edges"]}
        self.assertIn("PMR-a-chf", numbers)
        self.assertNotIn("PMR-b-chf", numbers)

    def test_payments_by_premiums_out_of_scope_left_out(self):
        receipts = self._receipts(
            f'{{ paymentsByPremiums(premiumUuids: ["{self.prem_a.uuid}", "{self.prem_b.uuid}"]) '
            f'{{ edges {{ node {{ receiptNo }} }} }} }}', "paymentsByPremiums", self.token)
        self.assertEqual({"PMR-a"}, receipts)

    def test_node_out_of_scope_not_found(self):
        from graphql_relay import to_global_id
        node_id = to_global_id("PaymentGQLType", self.pay_b.id)
        query = f'{{ node(id: "{node_id}") {{ ... on PaymentGQLType {{ receiptNo }} }} }}'
        content = self._q(query, self.token)
        self.assertIsNone((content.get("data") or {}).get("node"), content)

    def test_deleted_payments_only_on_request(self):
        Payment.objects.filter(id=self.pay_a.id).update(validity_to=datetime.datetime(2020, 1, 1))
        receipts = self._receipts('{ payments { edges { node { receiptNo } } } }', "payments", self.token)
        self.assertNotIn("PMR-a", receipts)
        history = self._receipts('{ payments(showHistory: true) { edges { node { receiptNo } } } }',
                                 "payments", self.token)
        self.assertIn("PMR-a", history)


@override_settings(ROW_SECURITY=True)
class PaymentWriteScopeTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        _build(cls, "PMW")

    def setUp(self):
        cache.clear()

    def _create(self, **extra):
        from payment.gql_mutations import CreatePaymentMutation
        return CreatePaymentMutation.async_mutate(self.user_a, **{
            "received_amount": 10, "receipt_no": "NEW", **extra})

    def test_create_on_a_premium_out_of_scope_refused(self):
        result = self._create(premium_uuid=self.prem_b.uuid, receipt_no="NEW-b")
        self.assertTrue(result, "refused")
        self.assertFalse(Payment.objects.filter(receipt_no="NEW-b").exists())
        self.assertEqual(1, PaymentDetail.objects.filter(premium=self.prem_b).count())

    def test_create_on_a_premium_in_scope_allowed(self):
        self.assertIsNone(self._create(premium_uuid=self.prem_a.uuid, receipt_no="NEW-a"))
        self.assertTrue(PaymentDetail.objects.filter(payment__receipt_no="NEW-a", premium=self.prem_a).exists())

    def test_create_with_the_uuid_of_another_payment_does_not_touch_it(self):
        """a uuid in the create input updated that payment, whoever it belonged to"""
        self._create(uuid=self.pay_b.uuid, receipt_no="HIJACKED")
        self.assertEqual("PMW-b", Payment.objects.get(id=self.pay_b.id).receipt_no)

    def test_create_with_the_id_of_another_payment_does_not_touch_it(self):
        """graphene does not enforce read_only: an id from the client is ignored"""
        self._create(id=self.pay_b.id, receipt_no="BY-ID")
        self.assertEqual("PMW-b", Payment.objects.get(id=self.pay_b.id).receipt_no)

    def test_update_out_of_scope_refused(self):
        from payment.gql_mutations import UpdatePaymentMutation
        result = UpdatePaymentMutation.async_mutate(self.user_a, uuid=self.pay_b.uuid, receipt_no="MOVED")
        self.assertTrue(result)
        self.assertEqual("PMW-b", Payment.objects.get(id=self.pay_b.id).receipt_no)

    def test_update_linking_a_premium_out_of_scope_refused(self):
        from payment.gql_mutations import UpdatePaymentMutation
        result = UpdatePaymentMutation.async_mutate(
            self.user_a, uuid=self.pay_a.uuid, receipt_no="PMW-a", premium_uuid=self.prem_b.uuid)
        self.assertTrue(result)
        self.assertFalse(PaymentDetail.objects.filter(payment=self.pay_a, premium=self.prem_b).exists())

    def test_update_in_scope_allowed(self):
        from payment.gql_mutations import UpdatePaymentMutation
        self.assertIsNone(UpdatePaymentMutation.async_mutate(self.user_a, uuid=self.pay_a.uuid, receipt_no="NEWNO"))
        self.assertEqual("NEWNO", Payment.objects.get(id=self.pay_a.id).receipt_no)

    def test_delete_out_of_scope_does_not_exist(self):
        from payment.gql_mutations import DeletePaymentsMutation
        result = DeletePaymentsMutation.async_mutate(self.user_a, uuids=[self.pay_b.uuid])
        self.assertIn("does_not_exist", _messages(result))
        self.assertIsNone(Payment.objects.get(id=self.pay_b.id).validity_to)

    def test_delete_in_scope_allowed(self):
        from payment.gql_mutations import DeletePaymentsMutation
        self.assertEqual([], DeletePaymentsMutation.async_mutate(self.user_a, uuids=[self.pay_a.uuid]))
        self.assertIsNotNone(Payment.objects.get(id=self.pay_a.id).validity_to)


@override_settings(ROW_SECURITY=True)
class DeletePoliciesDetachTest(TestCase):
    """The payment details of a policy deleted by DeletePoliciesMutation are detached, after its checks."""

    @classmethod
    def setUpTestData(cls):
        _build(cls, "PMD")

    def _signal(self, uuids):
        from payment.gql_mutations import on_policy_mutation
        return on_policy_mutation(None, mutation_class="DeletePoliciesMutation",
                                  data={"uuids": uuids}, user=self.user_a, error_messages=[])

    def test_bound_after_the_policy_mutation(self):
        import openIMIS.schema  # noqa: F401 -- builds the schema, which binds the modules' signals
        from core.schema import signal_mutation_module_before_mutating, signal_mutation_module_after_mutating
        from payment.gql_mutations import on_policy_mutation
        before = [r[1]() for r in signal_mutation_module_before_mutating["policy"].receivers]
        after = [r[1]() for r in signal_mutation_module_after_mutating["policy"].receivers]
        self.assertNotIn(on_policy_mutation, before)
        self.assertIn(on_policy_mutation, after)

    def test_details_of_a_policy_not_deleted_untouched(self):
        """the policy mutation refused it (out of scope): the policy is current, its payment stays matched"""
        self._signal([str(self.pol_b.uuid)])
        self.assertTrue(PaymentDetail.objects.filter(
            payment=self.pay_b, premium=self.prem_b, validity_to__isnull=True).exists())

    def test_details_of_a_deleted_policy_detached(self):
        Policy.objects.filter(id=self.pol_a.id).update(validity_to=datetime.datetime.now())
        self._signal([str(self.pol_a.uuid)])
        self.assertFalse(PaymentDetail.objects.filter(
            payment=self.pay_a, premium=self.prem_a, validity_to__isnull=True).exists())
