select region, sum(revenue) as revenue, sum(units) as units
from {{ ref('orders') }}
group by 1
