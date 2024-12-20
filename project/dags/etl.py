from datetime import datetime, timedelta
import os
import pandas as pd
from airflow import DAG
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.operators.python import PythonOperator
from airflow.utils.dates import days_ago


def get_all_tables():
    """Get list of all tables from source database"""
    pg_hook = PostgresHook(postgres_conn_id='northwind_db')
    sql = """
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = 'public'
    """
    return [row[0] for row in pg_hook.get_records(sql)]


def extract_postgres_data(execution_date):
    """Extract limited rows from all tables in PostgreSQL and save to local disk"""
    pg_hook = PostgresHook(postgres_conn_id='northwind_db')

    # Get all tables
    tables = get_all_tables()

    for table in tables:
        try:
            # Extract only 3 rows from each table
            sql = f"SELECT * FROM {table} LIMIT 3"
            df = pg_hook.get_pandas_df(sql)

            # Create directory for this execution date
            output_dir = f'/opt/airflow/data/postgres/{table}/{execution_date}'
            os.makedirs(output_dir, exist_ok=True)

            # Save to parquet
            output_file = f'{output_dir}/{table}.parquet'
            df.to_parquet(output_file, index=False)
            print(
                f"Successfully extracted {len(df)} rows from table {table} to {output_file}")

        except Exception as e:
            print(f"Error extracting table {table}: {e}")
            raise


def load_to_new_warehouse(execution_date):
    """Load orders data to warehouse"""
    pg_hook = PostgresHook(postgres_conn_id='new_postgres_db')

    # Load orders data
    orders_file = f'/opt/airflow/data/postgres/orders/{execution_date}/orders.parquet'
    orders_df = pd.read_parquet(orders_file)

    # Create warehouse_orders table if it doesn't exist
    create_tables_sql = """
    CREATE TABLE IF NOT EXISTS warehouse_orders (
        order_id INTEGER PRIMARY KEY,
        customer_id VARCHAR(50),
        order_date DATE,
        shipped_date DATE
    );
    """
    pg_hook.run(create_tables_sql)

    # Select relevant columns and handle duplicates
    filtered_df = orders_df[['order_id',
                             'customer_id', 'order_date', 'shipped_date']]
    filtered_df = filtered_df.copy()  # Create a copy to avoid SettingWithCopyWarning
    filtered_df.drop_duplicates(subset=['order_id'], inplace=True)

    # Insert data with upsert logic
    rows = filtered_df.to_dict('records')
    for row in rows:
        try:
            pg_hook.run("""
                INSERT INTO warehouse_orders 
                    (order_id, customer_id, order_date, shipped_date)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (order_id) 
                DO UPDATE SET 
                    customer_id = EXCLUDED.customer_id,
                    order_date = EXCLUDED.order_date,
                    shipped_date = EXCLUDED.shipped_date;
            """, parameters=(
                row['order_id'],
                row['customer_id'],
                row['order_date'],
                row['shipped_date']
            ))
        except Exception as e:
            print(f"Error processing order {row['order_id']}: {e}")
            raise


def load_csv_to_warehouse(execution_date):
    """Load limited CSV data to warehouse"""
    pg_hook = PostgresHook(postgres_conn_id='new_postgres_db')

    # Create directory for this execution date
    output_dir = f'/opt/airflow/data/csv/{execution_date}'
    os.makedirs(output_dir, exist_ok=True)

    # Read only 3 rows from CSV
    csv_path = '/opt/airflow/data/order_details.csv'
    df = pd.read_csv(csv_path, nrows=3)
    parquet_path = f'{output_dir}/order_details.parquet'
    df.to_parquet(parquet_path, index=False)

    print(f"Loaded {len(df)} rows from CSV file")

    # Create warehouse_order_details table if it doesn't exist
    create_table_sql = """
    CREATE TABLE IF NOT EXISTS warehouse_order_details (
        order_detail_id SERIAL PRIMARY KEY,
        order_id INTEGER,
        product_id INTEGER,
        unit_price DECIMAL(10, 2),
        quantity INTEGER,
        discount DECIMAL(4, 2),
        processed_date DATE DEFAULT CURRENT_DATE,
        FOREIGN KEY (order_id) REFERENCES warehouse_orders(order_id)
    );
    """
    pg_hook.run(create_table_sql)

    # Insert data with proper error handling
    for _, row in df.iterrows():
        try:
            pg_hook.run("""
                INSERT INTO warehouse_order_details 
                    (order_id, product_id, unit_price, quantity, discount)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING;
            """, parameters=(
                row['order_id'],
                row['product_id'],
                row['unit_price'],
                row['quantity'],
                row['discount']
            ))
        except Exception as e:
            print(
                f"Error processing order detail for order {row['order_id']}: {e}")
            raise


# Define the DAG
with DAG(
    'complete_postgres_etl',
    default_args={
        'owner': 'airflow',
        'start_date': days_ago(1),
        'retries': 1,
        'retry_delay': timedelta(minutes=5),
        'email_on_failure': True,
    },
    description='Complete ETL process for PostgreSQL with limited test data',
    schedule_interval='@daily',
    catchup=True,
    max_active_runs=1,
) as dag:

    extract_postgres_data_task = PythonOperator(
        task_id='extract_postgres_data',
        python_callable=extract_postgres_data,
        op_kwargs={'execution_date': '{{ ds }}'},
    )

    load_new_warehouse_task = PythonOperator(
        task_id='load_new_warehouse',
        python_callable=load_to_new_warehouse,
        op_kwargs={'execution_date': '{{ ds }}'},
    )

    load_csv_task = PythonOperator(
        task_id='load_csv_to_warehouse',
        python_callable=load_csv_to_warehouse,
        op_kwargs={'execution_date': '{{ ds }}'},
    )

    extract_postgres_data_task >> load_new_warehouse_task >> load_csv_task