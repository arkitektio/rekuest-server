from django.db import migrations, models

# A task assigned before policies existed keeps running as it always did: every child it asks
# for is dispatched at once (AUTO). From here on a new task asks (ASK) unless its root assign
# says otherwise. The column is added with AUTO, which fills the existing rows, and then
# defaults to ASK. The previous release never names the column, so it runs on this schema.


class Migration(migrations.Migration):

    dependencies = [
        ("facade", "0011_provided_bloks"),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="policy",
            field=models.CharField(
                choices=[
                    ("ASK", "Ask (an agent's child waits until a person approves it)"),
                    ("AUTO", "Auto (an agent's children run, and its own holds are approved, without asking)"),
                ],
                db_default="AUTO",
                default="AUTO",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="task",
            name="policy",
            field=models.CharField(
                choices=[
                    ("ASK", "Ask (an agent's child waits until a person approves it)"),
                    ("AUTO", "Auto (an agent's children run, and its own holds are approved, without asking)"),
                ],
                db_default="ASK",
                default="ASK",
                help_text="ASK: a child an agent assigns waits until a person approves it (HELD), and the task's own holds wait for a decision. AUTO: both are approved without asking. A root's is set on its assign; a child always takes its parent's.",
                max_length=20,
            ),
        ),
    ]
