from health_check.backends import BaseHealthCheckBackend

class HareHealthBackend(BaseHealthCheckBackend):
    #: The status endpoints will respond with a 200 status code
    #: even if the check errors.
    critical_service = False

    def check_status(self):
        # The test code goes here.
        # You can use `self.add_error` or
        # raise a `HealthCheckException`,
        # similar to Django's form validation.
        pass

    def identifier(self):
        return self.__class__.__name__  # Display name on the endpoint.