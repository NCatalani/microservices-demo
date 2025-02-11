#!/usr/bin/python
#
# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# NICOLAS
import elasticapm
from grpc_interceptor import ServerInterceptor
from grpc_interceptor.exceptions import GrpcException
from typing import Callable, Any
########
from concurrent import futures
import argparse
import os
import sys
import time
import grpc
import traceback
from jinja2 import Environment, FileSystemLoader, select_autoescape, TemplateError
from google.api_core.exceptions import GoogleAPICallError
from google.auth.exceptions import DefaultCredentialsError

import demo_pb2
import demo_pb2_grpc
from grpc_health.v1 import health_pb2
from grpc_health.v1 import health_pb2_grpc

from opentelemetry import trace
from opentelemetry.instrumentation.grpc import GrpcInstrumentorServer
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

import googlecloudprofiler

### NICOLAS
class ExceptionToStatusInterceptor(ServerInterceptor):
    def __init__(self):
        self.apm_client = elasticapm.Client(
                service_name=os.getenv("APM_SERVICE_NAME"),  # Add your service name here
                server_url=os.getenv("APM_SERVER_URL"),
                secret_token=os.getenv("APM_SECRET_TOKEN"),
                environment=os.getenv("APM_ENVIRONMENT")
        )

# This class, derived from GrpcInterceptor, handles incoming requests, catches errors, and reports them to Elastic APM.
    def intercept(
                self,
                method: Callable,
                request: Any,
                context: grpc.ServicerContext,
                method_name: str,
    ) -> Any:
        try:
        # Call the function and process the request
            return method(request, context)
        except (GrpcException, Exception, grpc.RpcError):
        # When an error is caught, send error information to Elastic APM
            self.apm_client.capture_exception()
        # Re-raise the error
            raise

######

from logger import getJSONLogger
logger = getJSONLogger('emailservice-server')

# Loads confirmation email template from file
env = Environment(
    loader=FileSystemLoader('templates'),
    autoescape=select_autoescape(['html', 'xml'])
)
template = env.get_template('confirmation.html')

class BaseEmailService(demo_pb2_grpc.EmailServiceServicer):
  def Check(self, request, context):
    return health_pb2.HealthCheckResponse(
      status=health_pb2.HealthCheckResponse.SERVING)

  def Watch(self, request, context):
    return health_pb2.HealthCheckResponse(
      status=health_pb2.HealthCheckResponse.UNIMPLEMENTED)

class EmailService(BaseEmailService):
  def __init__(self):
    raise Exception('cloud mail client not implemented')
    super().__init__()

  @staticmethod
  def send_email(client, email_address, content):
    response = client.send_message(
      sender = client.sender_path(project_id, region, sender_id),
      envelope_from_authority = '',
      header_from_authority = '',
      envelope_from_address = from_address,
      simple_message = {
        "from": {
          "address_spec": from_address,
        },
        "to": [{
          "address_spec": email_address
        }],
        "subject": "Your Confirmation Email",
        "html_body": content
      }
    )
    logger.info("Message sent: {}".format(response.rfc822_message_id))

  def SendOrderConfirmation(self, request, context):
    email = request.email
    order = request.order

    try:
      confirmation = template.render(order = order)
    except TemplateError as err:
      context.set_details("An error occurred when preparing the confirmation mail.")
      logger.error(err.message)
      context.set_code(grpc.StatusCode.INTERNAL)
      return demo_pb2.Empty()

    try:
      EmailService.send_email(self.client, email, confirmation)
    except GoogleAPICallError as err:
      context.set_details("An error occurred when sending the email.")
      print(err.message)
      context.set_code(grpc.StatusCode.INTERNAL)
      return demo_pb2.Empty()

    return demo_pb2.Empty()

class DummyEmailService(BaseEmailService):
  def SendOrderConfirmation(self, request, context):
    logger.info('A request to send order confirmation email to {} has been received.'.format(request.email))
    return demo_pb2.Empty()

class HealthCheck():
  def Check(self, request, context):
    return health_pb2.HealthCheckResponse(
      status=health_pb2.HealthCheckResponse.SERVING)

def start(dummy_mode):
  interceptors = [ExceptionToStatusInterceptor()]
  server = grpc.server(
    futures.ThreadPoolExecutor(max_workers=10),
    interceptors=interceptors,
  )
  service = None
  if dummy_mode:
    service = DummyEmailService()
  else:
    raise Exception('non-dummy mode not implemented yet')

  demo_pb2_grpc.add_EmailServiceServicer_to_server(service, server)
  health_pb2_grpc.add_HealthServicer_to_server(service, server)

  port = os.environ.get('PORT', "8080")
  logger.info("listening on port: "+port)
  server.add_insecure_port('[::]:'+port)
  server.start()
  try:
    while True:
      time.sleep(3600)
  except KeyboardInterrupt:
    server.stop(0)

def initStackdriverProfiling():
  project_id = None
  try:
    project_id = os.environ["GCP_PROJECT_ID"]
  except KeyError:
    # Environment variable not set
    pass

  for retry in range(1,4):
    try:
      if project_id:
        googlecloudprofiler.start(service='email_server', service_version='1.0.0', verbose=0, project_id=project_id)
      else:
        googlecloudprofiler.start(service='email_server', service_version='1.0.0', verbose=0)
      logger.info("Successfully started Stackdriver Profiler.")
      return
    except (BaseException) as exc:
      logger.info("Unable to start Stackdriver Profiler Python agent. " + str(exc))
      if (retry < 4):
        logger.info("Sleeping %d to retry initializing Stackdriver Profiler"%(retry*10))
        time.sleep (1)
      else:
        logger.warning("Could not initialize Stackdriver Profiler after retrying, giving up")
  return


if __name__ == '__main__':
  logger.info('starting the email service in dummy mode.')

  # Profiler
  try:
    if "DISABLE_PROFILER" in os.environ:
      raise KeyError()
    else:
      logger.info("Profiler enabled.")
      initStackdriverProfiling()
  except KeyError:
      logger.info("Profiler disabled.")

  # Tracing
  try:
    if os.environ["ENABLE_TRACING"] == "1":
      otel_endpoint = os.getenv("COLLECTOR_SERVICE_ADDR", "localhost:4317")
      trace.set_tracer_provider(TracerProvider())
      trace.get_tracer_provider().add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
            endpoint = otel_endpoint,
            insecure = True
          )
        )
      )
    grpc_server_instrumentor = GrpcInstrumentorServer()
    grpc_server_instrumentor.instrument()

  except (KeyError, DefaultCredentialsError):
      logger.info("Tracing disabled.")
  except Exception as e:
      logger.warn(f"Exception on Cloud Trace setup: {traceback.format_exc()}, tracing disabled.")

  start(dummy_mode = True)
