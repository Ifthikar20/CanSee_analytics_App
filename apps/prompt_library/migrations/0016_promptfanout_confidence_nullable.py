# Fan-out confidence becomes nullable: nothing computes one, so new rows
# store NULL instead of a fixed default. Existing rows keep their values.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("prompt_library", "0015_brandprompt_model_variants"),
    ]

    operations = [
        migrations.AlterField(
            model_name="promptfanout",
            name="confidence",
            field=models.FloatField(blank=True, null=True),
        ),
    ]
