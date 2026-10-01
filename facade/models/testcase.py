
from django.db.models.functions import Now
from django.db import models


class TestCase(models.Model):
    action = models.ForeignKey(
        "Action",
        on_delete=models.CASCADE,
        related_name="test_cases",
        help_text="The action this test belongs to",
    )
    tester = models.ForeignKey(
        "Action",
        on_delete=models.CASCADE,
        related_name="testing_cases",
        help_text="The action that is testing this test",
    )
    name = models.CharField(max_length=2000, null=True, blank=True)
    description = models.CharField(max_length=2000, null=True, blank=True)
    is_benchmark = models.BooleanField(default=False, db_default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["action", "tester"],
                name="No multiple Tests for same Action and Tester allowed",
            )
        ]


class TestResult(models.Model):
    case = models.ForeignKey(TestCase, on_delete=models.CASCADE, related_name="results")
    implementation = models.ForeignKey("Implementation", on_delete=models.CASCADE, related_name="testresults")
    tester = models.ForeignKey(
        "Implementation",
        on_delete=models.CASCADE,
        related_name="testing_results",
        help_text="The implementation that is testing this test",
    )
    passed = models.BooleanField(default=False, db_default=False)
    result = models.JSONField(default=dict, null=True, blank=True, db_default={})
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())
