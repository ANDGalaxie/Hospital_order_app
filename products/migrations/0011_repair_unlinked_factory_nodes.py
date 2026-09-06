from django.db import migrations


def get_descendant_ids(ProductCategory, root_id):
    descendant_ids = []
    current_ids = [root_id]

    while current_ids:
        child_ids = list(
            ProductCategory.objects.filter(
                parent_id__in=current_ids,
            ).values_list("id", flat=True)
        )

        if not child_ids:
            break

        descendant_ids.extend(child_ids)
        current_ids = child_ids

    return descendant_ids


def repair_unlinked_factory_nodes(apps, schema_editor):
    ProductCategory = apps.get_model(
        "products",
        "ProductCategory",
    )
    Product = apps.get_model(
        "products",
        "Product",
    )

    nodes = ProductCategory.objects.filter(
        node_type="factory",
        factory__isnull=True,
    )

    for node in nodes:
        category_ids = get_descendant_ids(
            ProductCategory,
            node.id,
        )

        factory_ids = list(
            Product.objects.filter(
                category_id__in=category_ids,
                factory__isnull=False,
            )
            .values_list("factory_id", flat=True)
            .distinct()
        )

        # 只有下面所有产品唯一指向同一个工厂时，才自动关联。
        if len(factory_ids) == 1:
            node.factory_id = factory_ids[0]
            node.save(update_fields=["factory"])


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0010_link_factory_nodes_from_products'),
    ]

    operations = [
        migrations.RunPython(
            repair_unlinked_factory_nodes,
            migrations.RunPython.noop,
        ),
    ]
