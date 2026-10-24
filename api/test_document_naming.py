from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase

from api.document_naming import default_document_name, person_name_from_fields


def f(field_id, value, **extra):
    return {"id": field_id, "type": "text", "currentValue": value, **extra}


class PersonNameTests(SimpleTestCase):
    def test_passenger_name(self):
        self.assertEqual(person_name_from_fields([f("Booking_Ref", "XK12P"), f("Passenger_Name", "John Doe")]), "John Doe")

    def test_surname_and_given_names_are_combined(self):
        fields = [f("Surname", "DOE"), f("Given_Name", "JANE MARY"), f("Nationality", "NIGERIAN")]
        self.assertEqual(person_name_from_fields(fields), "JANE MARY DOE")

    def test_first_middle_last(self):
        fields = [f("Last_Name", "Okafor"), f("First_Name", "Ada"), f("Middle_Name", "N.")]
        self.assertEqual(person_name_from_fields(fields), "Ada N. Okafor")

    def test_numbered_fields_prefer_the_first_person(self):
        fields = [f("Name2", "Second Person"), f("Name1", "First Person"), f("Name11", "Eleventh")]
        self.assertEqual(person_name_from_fields(fields), "First Person")

    def test_full_name_beats_parts(self):
        fields = [f("Surname", "Doe"), f("Full_Name", "John Q Doe")]
        self.assertEqual(person_name_from_fields(fields), "John Q Doe")

    def test_ignores_non_person_names(self):
        fields = [
            f("Company_Name", "Acme Ltd"), f("Bank_Name", "GTBank"), f("Airline_Name", "Delta"),
            f("Product_Name", "Phone"), f("Passenger_Email", "a@b.com"), f("Father_Name", "Old Man"),
            f("Gender", "Male", type="select", options=[{"value": "Male"}]),
        ]
        self.assertIsNone(person_name_from_fields(fields))

    def test_ignores_empty_and_non_name_values(self):
        fields = [f("Passenger_Name", ""), f("Full_Name", "1234567"), f("Name", "x")]
        self.assertIsNone(person_name_from_fields(fields))

    def test_ignores_dependent_copies(self):
        fields = [f("Name_Copy", "John", dependsOn="Name")]
        self.assertIsNone(person_name_from_fields(fields))


class DefaultDocumentNameTests(SimpleTestCase):
    template = SimpleNamespace(name="Boarding Pass")

    def test_uses_person_name(self):
        self.assertEqual(default_document_name(self.template, [f("Passenger_Name", "John Doe")]), "John Doe - Boarding Pass")

    def test_falls_back_to_my_template(self):
        self.assertEqual(default_document_name(self.template, [f("Seat", "12A")]), "My Boarding Pass")

    def test_no_template(self):
        self.assertEqual(default_document_name(None, []), "Untitled Document")


class DocumentNamingIntegrationTests(TestCase):
    def setUp(self):
        from accounts.models import User
        from api.models import Template, Tool

        self.user = User.objects.create_user("namer", "namer@example.com", "password")
        tool = Tool.objects.create(name="Flight Tool", price="5.00")
        self.template = Template.objects.create(
            name="Boarding Pass", type="tool", tool=tool,
            form_fields=[f("Passenger_Name", "JOHN DOE"), f("Seat", "12A")],
        )
        self.request = SimpleNamespace(user=self.user, data={})

    def create(self, updates, **extra):
        from api.serializers.purchases import PurchasedTemplateSerializer

        serializer = PurchasedTemplateSerializer(
            data={"template": str(self.template.id), "test": True, "field_updates": updates, **extra},
            context={"request": self.request},
        )
        serializer.is_valid(raise_exception=True)
        return serializer.save()

    def test_create_names_document_after_passenger(self):
        doc = self.create([{"id": "Passenger_Name", "value": "Ada Okafor"}, {"id": "Seat", "value": "3C"}])
        self.assertEqual(doc.name, "Ada Okafor - Boarding Pass")

    def test_explicit_name_wins(self):
        doc = self.create([{"id": "Passenger_Name", "value": "Ada Okafor"}], name="Trip to Lagos")
        self.assertEqual(doc.name, "Trip to Lagos")

    def test_update_refreshes_auto_name_but_not_custom_names(self):
        from api.serializers.purchases import PurchasedTemplateSerializer

        doc = self.create([{"id": "Passenger_Name", "value": "Ada Okafor"}])
        serializer = PurchasedTemplateSerializer(
            doc, data={"name": doc.name, "field_updates": [{"id": "Passenger_Name", "value": "Bola Ade"}]},
            partial=True, context={"request": self.request},
        )
        serializer.is_valid(raise_exception=True)
        doc = serializer.save()
        self.assertEqual(doc.name, "Bola Ade - Boarding Pass")

        doc.name = "My own name"
        doc.save()
        serializer = PurchasedTemplateSerializer(
            doc, data={"name": "My own name", "field_updates": [{"id": "Passenger_Name", "value": "Chi Obi"}]},
            partial=True, context={"request": self.request},
        )
        serializer.is_valid(raise_exception=True)
        self.assertEqual(serializer.save().name, "My own name")

    def test_model_create_names_and_inherits(self):
        # e.g. the AI-chat purchase path, which creates without a name
        from api.models import PurchasedTemplate

        doc = PurchasedTemplate.objects.create(
            buyer=self.user, template=self.template, form_fields=[f("Passenger_Name", "Kemi Bello")],
        )
        self.assertEqual(doc.name, "Kemi Bello - Boarding Pass")
        bare = PurchasedTemplate.objects.create(buyer=self.user, template=self.template)
        self.assertEqual(bare.name, "JOHN DOE - Boarding Pass")
        self.assertEqual(len(bare.form_fields), 2)
