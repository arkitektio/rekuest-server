from django.db import migrations, models

# Schedules and triggers fire runs nobody is watching. The ones written before policies keep
# doing what they always did: their runs hand every child on at once (AUTO), as 0012 decided for
# the tasks that existed. A rule written from here on asks (ASK) unless it says otherwise. The
# column is added with AUTO, which fills the existing rows, and then defaults to ASK. The
# previous release never names the column, so it runs on this schema.


class Migration(migrations.Migration):

    dependencies = [
        ("facade", "0012_task_policy"),
    ]

    operations = [
        migrations.AddField(
            model_name="schedule",
            name="policy",
            field=models.CharField(choices=[
                    ("ASK", "Ask (an agent's child waits until a person approves it)"),
                    ("AUTO", "Auto (an agent's children run, and its own holds are approved, without asking)"),
                ], db_default="AUTO", default="AUTO", max_length=20),
        ),
        migrations.AlterField(
            model_name="schedule",
            name="policy",
            field=models.CharField(
                choices=[
                    ("ASK", "Ask (an agent's child waits until a person approves it)"),
                    ("AUTO", "Auto (an agent's children run, and its own holds are approved, without asking)"),
                ],
                db_default="ASK",
                default="ASK",
                help_text='The policy of every run it creates: ASK holds a child an agent asks for until a person approves it, AUTO runs it unattended. A schedule runs while nobody watches, so AUTO is what a run that delegates needs.',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="trigger",
            name="policy",
            field=models.CharField(choices=[
                    ("ASK", "Ask (an agent's child waits until a person approves it)"),
                    ("AUTO", "Auto (an agent's children run, and its own holds are approved, without asking)"),
                ], db_default="AUTO", default="AUTO", max_length=20),
        ),
        migrations.AlterField(
            model_name="trigger",
            name="policy",
            field=models.CharField(
                choices=[
                    ("ASK", "Ask (an agent's child waits until a person approves it)"),
                    ("AUTO", "Auto (an agent's children run, and its own holds are approved, without asking)"),
                ],
                db_default="ASK",
                default="ASK",
                help_text='The policy of every run it fires: ASK holds a child an agent asks for until a person approves it, AUTO runs it unattended. The run itself is never held: the trigger, not an agent, asked for it.',
                max_length=20,
            ),
        ),
    ]
