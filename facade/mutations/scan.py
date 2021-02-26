from facade.scan import scan_service_for_models
from facade import types
from facade.models import DataPoint
from balder.types import BalderMutation
from herre import bounced
import graphene
import logging

logger = logging.getLogger(__name__)

class Scan(BalderMutation):
    """Scan allows you to add Datapoints to your Arnheim Schema, this is only available to Admin users"""

    class Arguments:
        host = graphene.String(description="The Base URL for the Datapoint you want to add", required=True)
        port = graphene.Int(description="The Port we should query on", required=False)
        force = graphene.Boolean(description="Force rescan on Datapoint", required=False)


    class Meta:
        type = types.DataQuery

    
    @bounced()
    def mutate(root, info, host=None, port = 8080, force=False):
        try:
            datapoint = DataPoint.objects.get(inward=host, port=port)

            if not force:
                return types.DataQuery(point=datapoint, models=datapoint.models.all())

        except DataPoint.DoesNotExist:
            logger.info("Didn't exist.. forcing scan!")

        
        datapoint, models = scan_service_for_models(host, port)
        

        return types.DataQuery(point=datapoint, models=models)
            

