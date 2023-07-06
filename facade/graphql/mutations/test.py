from facade import models
from facade import types
from balder.types import BalderMutation
from lok import bounced
import graphene
import logging

logger = logging.getLogger(__name__)



class CreateTestCase(BalderMutation):
    """Create Repostiory"""

    class Arguments:
        node = graphene.ID(description="The name of this template", required=True)
        key = graphene.String(description="The name of this template", required=True)
        name = graphene.String(description="The name of this testcase", required=True)
        description = graphene.String(description="The description of this testcase")
        is_benchmark = graphene.Boolean(description="Is this a benchmark?")

    class Meta:
        type = types.TestCase
        operation = "createTestCase"

    @bounced(anonymous=True)
    def mutate(root, info, node=None, key=None, description=None, is_benchmark=False, name=None):

        case, created = models.TestCase.objects.update_or_create(
            node_id=node, key=name, defaults=dict(name=name, is_benchmark=is_benchmark, description=description)
        )
        return case
    


class DeleteTestCaseResult(graphene.ObjectType):
    id = graphene.String()


class DeleteTestCase(BalderMutation):
    """Delete TestCase
    
    This mutation deletes an TestCase and returns the deleted TestCase."""

    class Arguments:
        id = graphene.ID(
            description="The ID of the testcase to delete",
            required=True,
        )

    @bounced()
    def mutate(root, info, id, **kwargs):
        case = models.TestCase.objects.get(id=id)
        case.delete()
        return {"id": id}

    class Meta:
        type = DeleteTestCaseResult



class CreateTestResult(BalderMutation):
    """Create Test Result"""

    class Arguments:
        template = graphene.ID(description="The associated template", required=True)
        case = graphene.ID(description="The associated case", required=True)
        passed = graphene.Boolean(description="Did the test-case pass", required=True)
        result = graphene.String(description="The result of the test")

    class Meta:
        type = types.TestResult


    @bounced(anonymous=True)
    def mutate(root, info, case=None, template=None, passed=None, result=None):

        result = models.TestResult.objects.create(
            case_id=case, template_id=template, passed=passed, result=result
        )
        return result
