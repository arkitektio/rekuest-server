from pydantic import BaseModel
from django.core.management import BaseCommand
from hare.hares.reserver import ReserverRabbit
import asyncio
from facade.models import Reservation, Provider, Provision


async def main(rabbit):
    # Perform connection
    await rabbit.connect()



class Command(BaseCommand):

    leave_locale_alone = True

    def add_arguments(self, parser):
        super(Command, self).add_arguments(parser)


    def handle(self, *args, **options):
        
        # Every provider needs to be unactive
        for provider in Provider.objects.all():
            provider.active == False