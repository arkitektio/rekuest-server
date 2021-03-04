from facade.scan import scan_service
from facade import types
from facade.models import  Service
from balder.types import BalderMutation
from herre import bounced
import graphene
import logging

logger = logging.getLogger(__name__)

class ScanMutation(BalderMutation):
    """Scan allows you to add Datapoints to your Arnheim Schema, this is only available to Admin users"""

    class Arguments:
        host = graphene.String(description="The Base URL for the Datapoint you want to add", required=True)
        port = graphene.Int(description="The Port we should query on", required=False)
        force = graphene.Boolean(description="Force rescan on Datapoint", required=False)


    class Meta:
        type = types.Service
        operation = "scan"

    
    @bounced()
    def mutate(root, info, host=None, port = 8080, force=False):
        try:
            service = Service.objects.get(inward=host, port=port)

            if not force:
                return service

        except Service.DoesNotExist:
            logger.info("Didn't exist.. forcing scan!")

        
        service = scan_service(host, port)
        

        return service
            

