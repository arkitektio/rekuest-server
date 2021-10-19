import re
from django.db.models.manager import Manager


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