import logging

logger = logging.getLogger(__name__)

def log_aiormq(func):

    def logged_func(self, message, *args, **kwargs):
        logger.info(f"Message in {self.__class__.__name__}: {str(message.body.decode())}")
        return func(self, message, *args, **kwargs)


    return logged_func