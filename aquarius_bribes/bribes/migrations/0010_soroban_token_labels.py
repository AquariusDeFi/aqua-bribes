from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bribes', '0009_auto_20250811_1004'),
    ]

    operations = [
        migrations.CreateModel(
            name='SorobanTokenSymbol',
            fields=[
                ('contract', models.CharField(max_length=56, primary_key=True, serialize=False)),
                ('symbol', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.AddField(
            model_name='marketkey',
            name='asset1_label',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='marketkey',
            name='asset2_label',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
    ]
