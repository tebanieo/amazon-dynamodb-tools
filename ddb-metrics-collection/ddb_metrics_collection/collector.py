"""
DynamoDB Metrics Collector

This module provides a class for collecting and analyzing DynamoDB table metrics across all AWS regions.
It focuses on identifying provisioned tables and their utilization metrics.

The DynamoDBMetricsCollector class offers methods to:
1. Fetch all AWS regions
2. Retrieve tables in each region
3. Determine the billing mode of each table
4. Collect metrics for provisioned tables
5. Analyze table utilization

The collector uses asynchronous operations to efficiently gather data from multiple regions and tables concurrently.

Dependencies:
    - asyncio: For asynchronous operations
    - aioboto3: For asynchronous AWS API calls
    - tqdm: For progress bars
    - logging: For error logging

Constants:
    MAX_CONCURRENT_REGIONS (int): Maximum number of regions to process concurrently
    MAX_CONCURRENT_TABLE_CHECKS (int): Maximum number of table checks to perform concurrently
"""

import asyncio
import aioboto3
from tqdm import tqdm
from tqdm.asyncio import tqdm_asyncio
from .metrics import get_metrics_for_table
import logging

MAX_CONCURRENT_REGIONS = 10
MAX_CONCURRENT_TABLE_CHECKS = 1000

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)


class DynamoDBMetricsCollector:
    """
    A class to collect and analyze DynamoDB table metrics across all AWS regions.
    """

    def __init__(self, config):
        self.session = aioboto3.Session()
        self.table_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TABLE_CHECKS)
        self.region_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REGIONS)
        self.config = config

    async def get_all_regions(self):
        """
        Fetch all available AWS regions.

        Returns:
            list: A list of AWS region names.
        """
        async with self.session.client("ec2") as ec2:
            response = await ec2.describe_regions()
            return [region["RegionName"] for region in response["Regions"]]

    async def get_tables_in_region(self, region):
        """
        Retrieve all DynamoDB tables in a specific region.

        Args:
            region (str): The AWS region name.

        Returns:
            list: A list of table names in the specified region.
        """
        async with self.session.client("dynamodb", region_name=region) as dynamodb:
            tables = []
            paginator = dynamodb.get_paginator("list_tables")
            try:
                async for page in paginator.paginate():
                    tables.extend(page.get("TableNames", []))
                return tables
            except Exception as e:
                logger.error(f"Error retrieving tables for region {region}: {str(e)}")
                return []

    async def get_table_billing_mode(self, region, table_name):
        """
        Determine the billing mode of a specific DynamoDB table.

        Args:
            region (str): The AWS region name.
            table_name (str): The name of the DynamoDB table.

        Returns:
            tuple: A tuple containing the table name and its billing mode.
        """
        async with self.table_semaphore:
            async with self.session.client("dynamodb", region_name=region) as dynamodb:
                try:
                    response = await dynamodb.describe_table(TableName=table_name)
                    return table_name, response["Table"].get(
                        "BillingModeSummary", {}
                    ).get("BillingMode", "PROVISIONED")
                except Exception as e:
                    logger.error(
                        f"Error getting billing mode for table {table_name} in region {region}: {str(e)}"
                    )
                    return table_name, None

    async def get_provisioned_tables(self, region):
        """
        Retrieve all provisioned tables in a specific region.

        Args:
            region (str): The AWS region name.

        Returns:
            list: A list of provisioned table names in the specified region.
        """
        tables = await self.get_tables_in_region(region)
        tasks = [self.get_table_billing_mode(region, table) for table in tables]
        results = await asyncio.gather(*tasks)
        return [
            table for table, billing_mode in results if billing_mode == "PROVISIONED"
        ]

    async def get_tables_and_metrics(self, region, start_time, end_time):
        """
        Retrieve provisioned tables and their metrics for a specific region.

        Args:
            region (str): The AWS region name.
            start_time (datetime): The start time for metric collection.
            end_time (datetime): The end time for metric collection.

        Returns:
            tuple: A tuple containing the region, provisioned tables, and their metrics.
        """
        async with self.region_semaphore:
            provisioned_tables = await self.get_provisioned_tables(region)
            tasks = [
                get_metrics_for_table(
                    self.session, table, region, start_time, end_time, self.config
                )
                for table in provisioned_tables
            ]
            table_metrics = await asyncio.gather(*tasks)
            return region, provisioned_tables, table_metrics

    async def collect_all_metrics(self, start_time, end_time):
        """
        Collect metrics for all provisioned DynamoDB tables across all regions.

        Args:
            start_time (datetime): The start time for metric collection.
            end_time (datetime): The end time for metric collection.

        Returns:
            tuple: A tuple containing all metrics and low utilization tables.
        """
        all_metrics = {}
        low_utilization_tables = {}

        print("Fetching all AWS regions...")
        regions = await self.get_all_regions()
        print(f"Found {len(regions)} regions.")

        print("Identifying provisioned tables in each region...")
        total_provisioned_tables = 0
        async for region in tqdm_asyncio(regions, desc="Scanning regions"):
            provisioned_tables = await self.get_provisioned_tables(region)
            total_provisioned_tables += len(provisioned_tables)

        print(
            f"Found {total_provisioned_tables} provisioned tables across all regions."
        )

        print("Collecting metrics for provisioned tables...")
        region_tasks = [
            self.get_tables_and_metrics(region, start_time, end_time)
            for region in regions
        ]

        with tqdm(total=total_provisioned_tables, desc="Collecting metrics") as pbar:
            for future in asyncio.as_completed(region_tasks):
                region, tables, table_metrics = await future
                all_metrics[region] = {}
                low_utilization_tables[region] = []

                for table, metrics in zip(tables, table_metrics):
                    if metrics:
                        all_metrics[region][table] = metrics
                        avg_read_util = self.calculate_average_utilization(
                            metrics, "read_utilization"
                        )
                        avg_write_util = self.calculate_average_utilization(
                            metrics, "write_utilization"
                        )

                        if self.is_low_utilization(avg_read_util, avg_write_util):
                            low_utilization_tables[region].append(
                                (table, avg_read_util, avg_write_util)
                            )
                    pbar.update(1)

        return all_metrics, low_utilization_tables

    @staticmethod
    def calculate_average_utilization(metrics, utilization_type):
        """
        Calculate the average utilization for a specific metric type.

        Args:
            metrics (list): List of metric dictionaries.
            utilization_type (str): The type of utilization to calculate ('read_utilization' or 'write_utilization').

        Returns:
            float or None: The average utilization, or None if no valid metrics are found.
        """
        valid_metrics = [
            m[utilization_type] for m in metrics if m[utilization_type] is not None
        ]
        return sum(valid_metrics) / len(valid_metrics) if valid_metrics else None

    @staticmethod
    def is_low_utilization(read_util, write_util, threshold=0.45):
        """
        Determine if a table has low utilization based on read and write utilization.

        Args:
            read_util (float or None): The read utilization value.
            write_util (float or None): The write utilization value.
            threshold (float): The utilization threshold for considering low utilization (default: 0.45).

        Returns:
            bool: True if the table has low utilization, False otherwise.
        """
        return (read_util is None or 0 <= read_util <= threshold) and (
            write_util is None or 0 <= write_util <= threshold
        )
