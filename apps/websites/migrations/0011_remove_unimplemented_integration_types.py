# Choices-only change: HubSpot, Facebook Ads, Shopify, Mailchimp, Google
# Drive and Canva were never implemented. Choices are not enforced by the
# database, so this is a no-op at the SQL level and existing rows are kept.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('websites', '0010_alter_website_unique_together_website_organization_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='integration',
            name='type',
            field=models.CharField(choices=[('ga', 'Google Analytics'), ('ga_hosted', 'Cansee Google tag'), ('cloudflare', 'Cloudflare'), ('gsc', 'Google Search Console')], max_length=20),
        ),
    ]
