import re
from typing import Any, List, Optional, Tuple
from django.db.models.manager import Manager
from hare.carrots import HareMessage
from hare.consumers.postman.protocols.postman_json import ReserveParams

qt = re.compile(r"@(?P<package>[^\/]*)\/(?P<interface>[^\/]*)")


class NodeManager(Manager):

    def get(self, q = None ,**kwargs ):
        """Takes an DataArray and the model arguments and returns the created Model
        
        Arguments:
            array {xr.DataArray} -- An xr.DataArray as a LarvikArray
        
        Returns:
            [models.Model] -- [The Model]
        """
        if q is not None:
            m = qt.match(q)
            if m:
                kwargs["package"] = m.group("package")
                kwargs["interface"]=m.group("interface")

        return super().get(**kwargs)





class ReservationManager(Manager):

    def schedule(self, params: Optional[ReserveParams] = None, 
    node: Optional[str] = None,
    template: Optional[str] = None,
    title: Optional[str]= None , waiter = None) -> Tuple[Any, List[HareMessage]]:
        """_summary_

        Args:
            params (Optional[dict]): _description_
            template (Optional[str]): _description_
            title (Optional[str]): _description_

        Returns:
            Tuple[Any, List[HareMessage]]: _description_
        """
        from .models import Provision, Template, Agent
        params: ReserveParams = params or ReserveParams() #TODO: Get default from settings or policy?

        provisions = list(Provision.objects.filter(template__node_id=node))

        t = Template.objects.filter(node_id=node).first()

        while len(provisions) < (params.minimalInstances or 1):

            provisions.append(Provision.objects.create(
                template=t,
                bound=Agent.objects.filter(registry=t.registry).first()
            ))


        res = super().create(node_id=node, template_id=template, waiter=waiter, params=params.dict())
        res.provisions.add(*provisions)
        res.save()

        return res, []