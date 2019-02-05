#!/usr/bin/env python
"""
Usage:

python redshift-unload-copy.py <config file> <region>


* Copyright 2014, Amazon.com, Inc. or its affiliates. All Rights Reserved.
*
* Licensed under the Amazon Software License (the "License").
* You may not use this file except in compliance with the License.
* A copy of the License is located at
*
* http://aws.amazon.com/asl/
*
* or in the "license" file accompanying this file. This file is distributed
* on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
* express or implied. See the License for the specific language governing
* permissions and limitations under the License.
"""

import json
import sys
import logging
from global_config import GlobalConfigParametersReader, config_parameters
from util.s3_utils import S3Helper, S3Details
from util.resources import ResourceFactory, TableResource, SchemaResource, DBResource
from util.tasks import TaskManager, FailIfResourceDoesNotExistsTask, CreateIfTargetDoesNotExistTask, \
    FailIfResourceClusterDoesNotExistsTask, UnloadDataToS3Task, CopyDataFromS3Task, CleanupS3StagingAreaTask, \
    NoOperationTask


region = None

encryptionKeyID = 'alias/RedshiftUnloadCopyUtility'


def usage():
    print("Redshift Unload/Copy Utility")
    print("Exports data from a source Redshift database to S3 as an encrypted dataset, "
          "and then imports into another Redshift Database")
    print("")
    print("Usage:")
    print("python redshift_unload_copy.py <configuration> <region>")
    print("    <configuration> Local Path or S3 Path to Configuration File on S3")
    print("    <region> Region where Configuration File is stored (S3) "
          "and where Master Keys and Data Exports are stored")
    sys.exit(-1)


class ConfigHelper:
    def __init__(self, config_path, s3_helper=None):
        self.s3_helper = s3_helper

        if config_path.startswith("s3://"):
            if s3_helper is None:
                raise Exception('No region set to get config file but it resides on S3')
            self.config = s3_helper.get_json_config_as_dict(config_path)
        else:
            with open(config_path) as f:
                self.config = json.load(f)


class UnloadCopyTool:
    # noinspection PyDefaultArgument
    def __init__(self,
                 config_file,
                 region_name,
                 global_config_values=GlobalConfigParametersReader().get_default_config_key_values()):
        for key, value in global_config_values.items():
            config_parameters[key] = value
        self.region = region_name
        self.s3_helper = S3Helper(self.region)

        # load the configuration
        self.config_helper = ConfigHelper(config_file, self.s3_helper)

        source = ResourceFactory.get_source_resource_from_config_helper(self.config_helper, self.region)

        destination = ResourceFactory.get_target_resource_from_config_helper(self.config_helper, self.region)

        self.task_manager = TaskManager()
        self.barrier_after_all_cluster_pre_tests = NoOperationTask()
        self.task_manager.add_task(self.barrier_after_all_cluster_pre_tests)
        self.barrier_after_all_resource_pre_tests = NoOperationTask()
        self.task_manager.add_task(self.barrier_after_all_resource_pre_tests)

        # TODO: Check whether both resources are of type table if that is not the case then perform other scenario's
        if isinstance(source, TableResource):
            if isinstance(destination, DBResource):
                if not isinstance(destination, TableResource):
                    destination = ResourceFactory.get_table_resource_from_merging_2_resources(destination, source)
                if global_config_values['tableName'] and global_config_values['tableName'] != 'None':
                    destination.set_table(global_config_values['tableName'])
                self.add_table_migration(source, destination, global_config_values)
            else:
                logging.fatal('Destination should be a database resource')
                raise NotImplementedError
        elif isinstance(source, SchemaResource):
            if not isinstance(destination, DBResource):
                logging.fatal('Destination should be a database resource')
                raise NotImplementedError
            self.add_schema_migration(source, destination, global_config_values)
        elif isinstance(source, DBResource):
            if not isinstance(destination, DBResource):
                logging.fatal('Destination should be a database resource')
                raise NotImplementedError
            self.add_database_migration(source, destination, global_config_values)
        else:
            # TODO: add additional scenario's
            # For example if both resources are of type schema then create target schema and migrate all tables
            logging.fatal('Source is not a Table, this type of unload-copy is currently not supported.')
            raise NotImplementedError

        self.task_manager.run(max_threads=4)

    def add_database_migration(self, source, destination, global_config_values):
        schemas = source.list_schemas()
        for schema in schemas:
            source_schema = SchemaResource(source.get_cluster(), schema)
            self.add_schema_migration(source_schema, destination, global_config_values)

    def add_schema_migration(self, source, destination, global_config_values):
        tables = source.list_tables()
        for table in tables:
            # Create an individual cluster instance for each task so that they can run in parallel
            source_cluster = ResourceFactory.get_cluster_from_cluster_dict(self.config_helper.config['unloadSource'],self.region)
            source_table = TableResource(source_cluster, source.get_schema(), table)
            destination_cluster = ResourceFactory.get_cluster_from_cluster_dict(self.config_helper.config['copyTarget'], self.region)
            target_table = ResourceFactory.get_table_resource_from_merging_2_resources(destination, source_table)
            target_table.set_cluster(destination_cluster)
            self.add_table_migration(source_table, target_table, global_config_values)

    def add_table_migration(self, source, destination, global_config_values):
        if 'explicit_ids' in self.config_helper.config['copyTarget']:
            if self.config_helper.config['copyTarget']['explicit_ids']:
                destination.set_explicit_ids(True)

        if global_config_values['connectionPreTest']:
            if not global_config_values['destinationTablePreTest']:
                destination_cluster_pre_test = FailIfResourceClusterDoesNotExistsTask(resource=destination)
                self.task_manager.add_task(destination_cluster_pre_test,
                                           dependency_of=self.barrier_after_all_cluster_pre_tests)
            if not global_config_values['sourceTablePreTest']:
                source_cluster_pre_test = FailIfResourceClusterDoesNotExistsTask(resource=source)
                self.task_manager.add_task(source_cluster_pre_test,
                                           dependency_of=self.barrier_after_all_cluster_pre_tests)
        if global_config_values['destinationTablePreTest']:
            if global_config_values['destinationTableAutoCreate']:
                destination_cluster_pre_test = FailIfResourceClusterDoesNotExistsTask(resource=destination)
                self.task_manager.add_task(destination_cluster_pre_test,
                                           dependency_of=self.barrier_after_all_cluster_pre_tests)
            else:
                destination_table_pre_test = FailIfResourceDoesNotExistsTask(destination)
                self.task_manager.add_task(destination_table_pre_test,
                                           dependency_of=self.barrier_after_all_resource_pre_tests,
                                           dependencies=self.barrier_after_all_cluster_pre_tests)

        if global_config_values['sourceTablePreTest']:
            source_table_pre_test = FailIfResourceDoesNotExistsTask(source)
            self.task_manager.add_task(source_table_pre_test,
                                       dependency_of=self.barrier_after_all_resource_pre_tests,
                                       dependencies=self.barrier_after_all_cluster_pre_tests)

        if global_config_values['destinationTableAutoCreate']:
            create_target = CreateIfTargetDoesNotExistTask(
                source_resource=source,
                target_resource=destination
            )
            self.task_manager.add_task(create_target,
                                       dependency_of=self.barrier_after_all_resource_pre_tests,
                                       dependencies=self.barrier_after_all_cluster_pre_tests)

        s3_details = S3Details(self.config_helper, source, encryption_key_id=encryptionKeyID)
        unload_data = UnloadDataToS3Task(source, s3_details)
        self.task_manager.add_task(unload_data, dependencies=self.barrier_after_all_resource_pre_tests)

        copy_data = CopyDataFromS3Task(destination, s3_details)
        self.task_manager.add_task(copy_data, dependencies=unload_data)

        s3_cleanup = CleanupS3StagingAreaTask(s3_details)
        self.task_manager.add_task(s3_cleanup, dependencies=copy_data)


def set_log_level(log_level_string):
    log_level_string = log_level_string.upper()
    if not hasattr(logging, log_level_string):
        logging.error('Could not find log_level {lvl}'.format(lvl=log_level_string))
        logging.basicConfig(level=logging.INFO)
    else:
        format_str = '[%(asctime)s] [%(threadName)s] %(levelname)s - %(message)s'
        logging.basicConfig(level=log_level_string, format=format_str)
        logging.debug('Log level set to {lvl}'.format(lvl=log_level_string))


def main(args):
    global region

    global_config_reader = GlobalConfigParametersReader()
    global_config_values = global_config_reader.get_config_key_values_updated_with_cli_args(args)
    set_log_level(global_config_values['logLevel'])

    UnloadCopyTool(global_config_values['s3ConfigFile'], global_config_values['region'], global_config_values)

if __name__ == "__main__":
    main(sys.argv)
