from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('rewards', '0013_ahbs_asset_date_idx')]
    operations = [
        migrations.CreateModel(
            name='ContractDelegationSnapshot',
            fields=[
                ('day', models.DateField(primary_key=True, serialize=False)),
                ('source_id', models.CharField(max_length=64, unique=True)),
                ('payload', models.JSONField()),
                ('votes_loaded', models.BooleanField(default=False)),
                ('loaded_markets', models.JSONField(default=list)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
    ]
