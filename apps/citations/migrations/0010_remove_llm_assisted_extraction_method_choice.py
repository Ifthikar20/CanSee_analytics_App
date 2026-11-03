# Choices-only change: the LLM-assisted extractor was a stub that never
# produced a citation, so no row holds "llm_assisted". No SQL is emitted.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("citations", "0009_sourcescanengineanswer"),
    ]

    operations = [
        migrations.AlterField(
            model_name="citation",
            name="extraction_method",
            field=models.CharField(
                choices=[
                    ("native", "Native"),
                    ("grounding", "Grounding"),
                    ("regex", "Regex"),
                ],
                default="regex",
                max_length=16,
            ),
        ),
    ]
