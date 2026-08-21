import logging
from app.core.database import SessionLocal
from app.models.product import Product, ProductPricing
from app.models.organization import Organization

logger = logging.getLogger("crm.product_pricing")


def backfill_product_pricing() -> None:
    """Migrate legacy products without ProductPricing records.

    For existing products with price > 0, creates a corresponding ProductPricing
    record preserving legacy pricing values and organization currency.
    """
    db = SessionLocal()
    try:
        unpriced_products = (
            db.query(Product)
            .outerjoin(ProductPricing, Product.id == ProductPricing.product_id)
            .filter(ProductPricing.id == None)
            .all()
        )
        if not unpriced_products:
            return

        migrated_count = 0
        for prod in unpriced_products:
            if prod.price and prod.price > 0:
                org = db.get(Organization, prod.organization_id)
                currency = prod.currency or (org.currency if org and org.currency else "INR")
                pricing = ProductPricing(
                    product_id=prod.id,
                    purchase_price=prod.price,
                    selling_price=prod.price,
                    mrp=prod.msrp_mrp,
                    wholesale_price=prod.wholesale_price,
                    dealer_price=prod.dealer_price,
                    discount_percent=prod.discount or prod.default_discount,
                    tax_inclusive=prod.tax_inclusive or prod.tax_inclusive_price,
                    currency=currency,
                )
                db.add(pricing)
                migrated_count += 1

        if migrated_count > 0:
            db.commit()
            logger.info("Migrated pricing for %d existing products", migrated_count)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("Failed to backfill product pricing")
    finally:
        db.close()
