select
    id as order_id,
    customer_id,
    order_date,
    status,
    order_total
from {{ ref('raw_orders') }}
