from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("brand_vault", "0013_brandpulse"),
    ]

    operations = [
        migrations.AlterField(
            model_name="safetyalert",
            name="issue",
            field=models.CharField(
                choices=[
                    ("hallucination", "Hallucination"),
                    ("unverified", "Unverified claim"),
                    ("outdated", "Outdated info"),
                    ("harmful", "Harmful mention"),
                    ("negative", "Negative mention"),
                    ("emerging_narrative", "Emerging narrative"),
                    ("negative_outranking", "Negative page outranking"),
                    ("ranking_for_bad_query", "Ranking for negative query"),
                    ("sge_misrepresentation", "AI Overview misrepresentation"),
                    ("sentiment_drop", "Sentiment drop"),
                    ("impersonation", "Impersonation"),
                    ("derogatory", "Derogatory language"),
                    ("unfavorable_comparison", "Unfavorable comparison"),
                    ("weak_endorsement", "Weak endorsement"),
                    ("distrust", "Distrust signals"),
                    ("private_data", "Private data exposure"),
                    ("hallucinated_compliance", "Hallucinated compliance claim"),
                    ("false_incident", "Unconfirmed security incident"),
                    ("security_advisory", "Advised against on security grounds"),
                ],
                max_length=32,
            ),
        ),
    ]
