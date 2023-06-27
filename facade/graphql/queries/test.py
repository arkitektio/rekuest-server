from facade.filters import TestCaseFilter, TestResultFilter
from balder.types import BalderQuery
from facade import types
from facade.models import TestCase, TestResult
import graphene
from lok import bounced
from guardian.shortcuts import get_objects_for_user
from facade.inputs import ProvisionStatusInput

class TestCaseDetailQuery(BalderQuery):
    class Arguments:
        id = graphene.ID(description="The query pod")

    @bounced(anonymous=True)
    def resolve(root, info, id=None):
        return TestCase.objects.get(id=id)

    class Meta:
        type = types.TestCase
        operation = "testcase"


class Testcases(BalderQuery):
    class Meta:
        type = types.TestCase
        list = True
        paginate = True
        filter = TestCaseFilter



class TestResultDetailQuery(BalderQuery):
    class Arguments:
        id = graphene.ID(description="The query pod")

    @bounced(anonymous=True)
    def resolve(root, info, id=None):
        return TestResult.objects.get(id=id)

    class Meta:
        type = types.TestResult
        operation = "testresult"


class Testresults(BalderQuery):
    class Meta:
        type = types.TestResult
        list = True
        paginate = True
        filter = TestResultFilter

