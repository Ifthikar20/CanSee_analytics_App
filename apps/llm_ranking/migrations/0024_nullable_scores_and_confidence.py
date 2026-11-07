# Scores and confidence become nullable so a run with no answered query,
# or a value nothing computes, is stored as NULL instead of 0. Only the
# NOT NULL constraints change; existing rows keep their values.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("llm_ranking", "0023_alter_llmrankingschedule_created_by"),
    ]

    operations = [
        migrations.AlterField(
            model_name="llmrankingaudit",
            name="overall_score",
            field=models.IntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AlterField(
            model_name="llmrankingaudit",
            name="mention_rate",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="llmrankingaudit",
            name="mention_rate_smoothed",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="llmrankingaudit",
            name="avg_mention_rank",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="llmrankingaudit",
            name="mention_rate_ci_lower",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="llmrankingaudit",
            name="mention_rate_ci_upper",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="llmrankingresult",
            name="confidence_score",
            field=models.FloatField(blank=True, null=True),
        ),
    ]
