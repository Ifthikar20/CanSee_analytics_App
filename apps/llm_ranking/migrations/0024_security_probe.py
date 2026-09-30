from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("llm_ranking", "0023_alter_llmrankingschedule_created_by"),
    ]

    operations = [
        migrations.AddField(
            model_name="llmrankingaudit",
            name="probe_kind",
            field=models.CharField(
                choices=[("visibility", "Visibility"), ("security", "Security perception")],
                db_index=True,
                default="visibility",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="llmrankingresult",
            name="security_claims",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
