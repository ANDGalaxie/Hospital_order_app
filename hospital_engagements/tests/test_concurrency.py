from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import TransactionTestCase

from hospitals.models import Hospital
from hospital_engagements.models import HospitalContact, HospitalEngagement, HospitalFollowUp
from hospital_engagements.services import change_hospital_engagement_stage, save_contact


@skipUnless(connection.vendor == "postgresql", "Row-lock concurrency requires PostgreSQL")
class PostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.hospital = Hospital.objects.create(name="Concurrent hospital")
        self.user = get_user_model().objects.create_user("concurrent-staff", is_staff=True)

    def run_concurrently(self, action):
        barrier = Barrier(2)

        def worker(index):
            try:
                barrier.wait(timeout=10)
                return action(index)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, index) for index in range(2)]
            return [future.result(timeout=20) for future in futures]

    def test_two_first_primary_contacts_are_serialized(self):
        hospital_id = self.hospital.pk

        def action(index):
            contact = HospitalContact(hospital_id=hospital_id, name=f"Contact {index}", is_primary=True)
            return save_contact(contact).pk

        self.run_concurrently(action)
        self.assertEqual(HospitalContact.objects.filter(hospital=self.hospital).count(), 2)
        self.assertEqual(HospitalContact.objects.filter(
            hospital=self.hospital, is_active=True, is_primary=True
        ).count(), 1)

    def test_concurrent_identical_stage_changes_create_one_history(self):
        engagement_id = self.hospital.engagement.pk
        user_id = self.user.pk

        def action(index):
            engagement = HospitalEngagement.objects.get(pk=engagement_id)
            user = get_user_model().objects.get(pk=user_id)
            return change_hospital_engagement_stage(engagement, "stage_2", user).pk

        self.run_concurrently(action)
        self.assertEqual(HospitalEngagement.objects.get(pk=engagement_id).stage, "stage_2")
        self.assertEqual(HospitalFollowUp.objects.filter(
            engagement_id=engagement_id, activity_type="stage_change"
        ).count(), 1)
