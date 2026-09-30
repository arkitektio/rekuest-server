"""Test cases (an action tested by another) and their results."""

import logging

import strawberry
from kante.types import Info

from facade import models, types
from facade.types.base import scoped_get
from rekuest_core import scalars as rscalars

logger = logging.getLogger(__name__)


@strawberry.input(description="Declare that one action tests another.")
class CreateTestCaseInput:
    action: strawberry.ID = strawberry.field(description="The action under test.")
    tester: strawberry.ID = strawberry.field(description="The action that performs the test.")
    description: str | None = None
    name: str | None = None
    is_benchmark: bool = strawberry.field(default=False, description="Measures performance rather than correctness.")


def create_test_case(info: Info, input: CreateTestCaseInput) -> types.TestCase:
    """Create (or update) the test case of ``tester`` for ``action``, both in the caller's organization."""
    test_case, _ = models.TestCase.objects.update_or_create(
        action=scoped_get(models.Action, info, input.action),
        tester=scoped_get(models.Action, info, input.tester),
        defaults=dict(description=input.description, name=input.name, is_benchmark=input.is_benchmark),
    )
    return test_case


@strawberry.input(description="Record one run of a test case.")
class CreateTestResultInput:
    case: strawberry.ID
    tester: strawberry.ID = strawberry.field(description="The implementation that ran the test.")
    implementation: strawberry.ID = strawberry.field(description="The implementation under test.")
    passed: bool
    result: rscalars.AnyDefault | None = strawberry.field(default=None, description="What the test produced, as JSON.")


def create_test_result(info: Info, input: CreateTestResultInput) -> types.TestResult:
    """Record a result; the case and both implementations must be in the caller's organization."""
    return models.TestResult.objects.create(
        case=scoped_get(models.TestCase, info, input.case, field="action__organization"),
        implementation=scoped_get(models.Implementation, info, input.implementation, field="agent__organization"),
        tester=scoped_get(models.Implementation, info, input.tester, field="agent__organization"),
        passed=input.passed,
        result=input.result,
    )
