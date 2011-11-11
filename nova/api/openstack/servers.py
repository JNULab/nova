# Copyright 2010 OpenStack LLC.
# Copyright 2011 Piston Cloud Computing, Inc
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import base64
import os
import traceback

from lxml import etree
from webob import exc
import webob
from xml.dom import minidom

import nova.api.openstack
from nova.api.openstack import common
from nova.api.openstack import ips
from nova.api.openstack.views import addresses as views_addresses
from nova.api.openstack.views import flavors as views_flavors
from nova.api.openstack.views import images as views_images
from nova.api.openstack.views import servers as views_servers
from nova.api.openstack import wsgi
from nova.api.openstack import xmlutil
from nova import compute
from nova.compute import instance_types
from nova import network
from nova import db
from nova import exception
from nova import flags
from nova import image
from nova import log as logging
from nova.rpc import common as rpc_common
from nova.scheduler import api as scheduler_api
from nova import utils


LOG = logging.getLogger('nova.api.openstack.servers')
FLAGS = flags.FLAGS


class ConvertedException(exc.WSGIHTTPException):
    def __init__(self, code, title, explanation):
        self.code = code
        self.title = title
        self.explanation = explanation
        super(ConvertedException, self).__init__()


class Controller(object):
    """ The Server API base controller class for the OpenStack API """

    def __init__(self):
        self.compute_api = compute.API()
        self.network_api = network.API()

    def index(self, req):
        """ Returns a list of server names and ids for a given user """
        try:
            servers = self._get_servers(req, is_detail=False)
        except exception.Invalid as err:
            raise exc.HTTPBadRequest(explanation=str(err))
        except exception.NotFound:
            raise exc.HTTPNotFound()
        return servers

    def detail(self, req):
        """ Returns a list of server details for a given user """
        try:
            servers = self._get_servers(req, is_detail=True)
        except exception.Invalid as err:
            raise exc.HTTPBadRequest(explanation=str(err))
        except exception.NotFound as err:
            raise exc.HTTPNotFound()
        return servers

    def _get_block_device_mapping(self, data):
        """Get block_device_mapping from 'server' dictionary.
        Overridden by volumes controller.
        """
        return None

    def _get_servers(self, req, is_detail):
        """Returns a list of servers, taking into account any search
        options specified.
        """

        search_opts = {}
        search_opts.update(req.str_GET)

        context = req.environ['nova.context']
        remove_invalid_options(context, search_opts,
                self._get_server_search_options())

        # Convert local_zone_only into a boolean
        search_opts['local_zone_only'] = utils.bool_from_str(
                search_opts.get('local_zone_only', False))

        # If search by 'status', we need to convert it to 'vm_state'
        # to pass on to child zones.
        if 'status' in search_opts:
            status = search_opts['status']
            state = common.vm_state_from_status(status)
            if state is None:
                reason = _('Invalid server status: %(status)s') % locals()
                raise exception.InvalidInput(reason=reason)
            search_opts['vm_state'] = state

        if 'changes-since' in search_opts:
            try:
                parsed = utils.parse_isotime(search_opts['changes-since'])
            except ValueError:
                msg = _('Invalid changes-since value')
                raise exc.HTTPBadRequest(explanation=msg)
            search_opts['changes-since'] = parsed

        # By default, compute's get_all() will return deleted instances.
        # If an admin hasn't specified a 'deleted' search option, we need
        # to filter out deleted instances by setting the filter ourselves.
        # ... Unless 'changes-since' is specified, because 'changes-since'
        # should return recently deleted images according to the API spec.

        if 'deleted' not in search_opts:
            if 'changes-since' not in search_opts:
                # No 'changes-since', so we only want non-deleted servers
                search_opts['deleted'] = False

        instance_list = self.compute_api.get_all(context,
                                                 search_opts=search_opts)

        limited_list = self._limit_items(instance_list, req)
        return self._build_list(req, limited_list, is_detail=is_detail)

    def _get_server(self, context, instance_uuid):
        """Utility function for looking up an instance by uuid"""
        try:
            return self.compute_api.routing_get(context, instance_uuid)
        except exception.NotFound:
            raise exc.HTTPNotFound()

    def _handle_quota_error(self, error):
        """
        Reraise quota errors as api-specific http exceptions
        """

        code_mappings = {
            "OnsetFileLimitExceeded":
                    _("Personality file limit exceeded"),
            "OnsetFilePathLimitExceeded":
                    _("Personality file path too long"),
            "OnsetFileContentLimitExceeded":
                    _("Personality file content too long"),
            "InstanceLimitExceeded":
                    _("Instance quotas have been exceeded")}

        expl = code_mappings.get(error.code)
        if expl:
            raise exc.HTTPRequestEntityTooLarge(explanation=expl,
                                                headers={'Retry-After': 0})
        # if the original error is okay, just reraise it
        raise error

    def _deserialize_create(self, request):
        """
        Deserialize a create request

        Overrides normal behavior in the case of xml content
        """
        if request.content_type == "application/xml":
            deserializer = ServerXMLDeserializer()
            return deserializer.deserialize(request.body)
        else:
            return self._deserialize(request.body, request.get_content_type())

    def _validate_server_name(self, value):
        if not isinstance(value, basestring):
            msg = _("Server name is not a string or unicode")
            raise exc.HTTPBadRequest(explanation=msg)

        if value.strip() == '':
            msg = _("Server name is an empty string")
            raise exc.HTTPBadRequest(explanation=msg)

    def _get_injected_files(self, personality):
        """
        Create a list of injected files from the personality attribute

        At this time, injected_files must be formatted as a list of
        (file_path, file_content) pairs for compatibility with the
        underlying compute service.
        """
        injected_files = []

        for item in personality:
            try:
                path = item['path']
                contents = item['contents']
            except KeyError as key:
                expl = _('Bad personality format: missing %s') % key
                raise exc.HTTPBadRequest(explanation=expl)
            except TypeError:
                expl = _('Bad personality format')
                raise exc.HTTPBadRequest(explanation=expl)
            try:
                contents = base64.b64decode(contents)
            except TypeError:
                expl = _('Personality content for %s cannot be decoded') % path
                raise exc.HTTPBadRequest(explanation=expl)
            injected_files.append((path, contents))
        return injected_files

    def _get_server_admin_password_old_style(self, server):
        """ Determine the admin password for a server on creation """
        return utils.generate_password(FLAGS.password_length)

    def _get_server_admin_password_new_style(self, server):
        """ Determine the admin password for a server on creation """
        password = server.get('adminPass')

        if password is None:
            return utils.generate_password(FLAGS.password_length)
        if not isinstance(password, basestring) or password == '':
            msg = _("Invalid adminPass")
            raise exc.HTTPBadRequest(explanation=msg)
        return password

    def _get_requested_networks(self, requested_networks):
        """
        Create a list of requested networks from the networks attribute
        """
        networks = []
        for network in requested_networks:
            try:
                network_uuid = network['uuid']

                if not utils.is_uuid_like(network_uuid):
                    msg = _("Bad networks format: network uuid is not in"
                         " proper format (%s)") % network_uuid
                    raise exc.HTTPBadRequest(explanation=msg)

                #fixed IP address is optional
                #if the fixed IP address is not provided then
                #it will use one of the available IP address from the network
                address = network.get('fixed_ip', None)
                if address is not None and not utils.is_valid_ipv4(address):
                    msg = _("Invalid fixed IP address (%s)") % address
                    raise exc.HTTPBadRequest(explanation=msg)
                # check if the network id is already present in the list,
                # we don't want duplicate networks to be passed
                # at the boot time
                for id, ip in networks:
                    if id == network_uuid:
                        expl = _("Duplicate networks (%s) are not allowed")\
                                % network_uuid
                        raise exc.HTTPBadRequest(explanation=expl)

                networks.append((network_uuid, address))
            except KeyError as key:
                expl = _('Bad network format: missing %s') % key
                raise exc.HTTPBadRequest(explanation=expl)
            except TypeError:
                expl = _('Bad networks format')
                raise exc.HTTPBadRequest(explanation=expl)

        return networks

    def _validate_user_data(self, user_data):
        """Check if the user_data is encoded properly"""
        if not user_data:
            return
        try:
            user_data = base64.b64decode(user_data)
        except TypeError:
            expl = _('Userdata content cannot be decoded')
            raise exc.HTTPBadRequest(explanation=expl)

    @exception.novaclient_converter
    @scheduler_api.redirect_handler
    def show(self, req, id):
        """ Returns server details by server id """
        try:
            instance = self.compute_api.routing_get(
                req.environ['nova.context'], id)
            return self._build_view(req, instance, is_detail=True)
        except exception.NotFound:
            raise exc.HTTPNotFound()

    def create(self, req, body):
        """ Creates a new server for a given user """

        if not body:
            raise exc.HTTPUnprocessableEntity()

        if not 'server' in body:
            raise exc.HTTPUnprocessableEntity()

        body['server']['key_name'] = self._get_key_name(req, body)

        context = req.environ['nova.context']
        server_dict = body['server']
        password = self._get_server_admin_password(server_dict)

        if not 'name' in server_dict:
            msg = _("Server name is not defined")
            raise exc.HTTPBadRequest(explanation=msg)

        name = server_dict['name']
        self._validate_server_name(name)
        name = name.strip()

        image_href = self._image_ref_from_req_data(body)

        # If the image href was generated by nova api, strip image_href
        # down to an id and use the default glance connection params
        if str(image_href).startswith(req.application_url):
            image_href = image_href.split('/').pop()

        personality = server_dict.get('personality')
        config_drive = server_dict.get('config_drive')

        injected_files = []
        if personality:
            injected_files = self._get_injected_files(personality)

        sg_names = []
        security_groups = server_dict.get('security_groups')
        if security_groups is not None:
            sg_names = [sg['name'] for sg in security_groups if sg.get('name')]
        if not sg_names:
            sg_names.append('default')

        sg_names = list(set(sg_names))

        requested_networks = server_dict.get('networks')
        if requested_networks is not None:
            requested_networks = self._get_requested_networks(
                                                    requested_networks)

        try:
            flavor_id = self._flavor_id_from_req_data(body)
        except ValueError as error:
            msg = _("Invalid flavorRef provided.")
            raise exc.HTTPBadRequest(explanation=msg)

        zone_blob = server_dict.get('blob')

        # optional openstack extensions:
        key_name = server_dict.get('key_name')
        user_data = server_dict.get('user_data')
        self._validate_user_data(user_data)

        availability_zone = server_dict.get('availability_zone')
        name = server_dict['name']
        self._validate_server_name(name)
        name = name.strip()

        block_device_mapping = self._get_block_device_mapping(server_dict)

        # Only allow admins to specify their own reservation_ids
        # This is really meant to allow zones to work.
        reservation_id = server_dict.get('reservation_id')
        if all([reservation_id is not None,
                reservation_id != '',
                not context.is_admin]):
            reservation_id = None

        ret_resv_id = server_dict.get('return_reservation_id', False)

        min_count = server_dict.get('min_count')
        max_count = server_dict.get('max_count')
        # min_count and max_count are optional.  If they exist, they come
        # in as strings.  We want to default 'min_count' to 1, and default
        # 'max_count' to be 'min_count'.
        min_count = int(min_count) if min_count else 1
        max_count = int(max_count) if max_count else min_count
        if min_count > max_count:
            min_count = max_count

        auto_disk_config = server_dict.get('auto_disk_config')

        try:
            inst_type = \
                    instance_types.get_instance_type_by_flavor_id(flavor_id)

            (instances, resv_id) = self.compute_api.create(context,
                            inst_type,
                            image_href,
                            display_name=name,
                            display_description=name,
                            key_name=key_name,
                            metadata=server_dict.get('metadata', {}),
                            access_ip_v4=server_dict.get('accessIPv4'),
                            access_ip_v6=server_dict.get('accessIPv6'),
                            injected_files=injected_files,
                            admin_password=password,
                            zone_blob=zone_blob,
                            reservation_id=reservation_id,
                            min_count=min_count,
                            max_count=max_count,
                            requested_networks=requested_networks,
                            security_group=sg_names,
                            user_data=user_data,
                            availability_zone=availability_zone,
                            config_drive=config_drive,
                            block_device_mapping=block_device_mapping,
                            auto_disk_config=auto_disk_config)
        except exception.QuotaError as error:
            self._handle_quota_error(error)
        except exception.InstanceTypeMemoryTooSmall as error:
            raise exc.HTTPBadRequest(explanation=unicode(error))
        except exception.InstanceTypeDiskTooSmall as error:
            raise exc.HTTPBadRequest(explanation=unicode(error))
        except exception.ImageNotFound as error:
            msg = _("Can not find requested image")
            raise exc.HTTPBadRequest(explanation=msg)
        except exception.FlavorNotFound as error:
            msg = _("Invalid flavorRef provided.")
            raise exc.HTTPBadRequest(explanation=msg)
        except exception.KeypairNotFound as error:
            msg = _("Invalid key_name provided.")
            raise exc.HTTPBadRequest(explanation=msg)
        except exception.SecurityGroupNotFound as error:
            raise exc.HTTPBadRequest(explanation=unicode(error))
        except rpc_common.RemoteError as err:
            msg = "%(err_type)s: %(err_msg)s" % \
                  {'err_type': err.exc_type, 'err_msg': err.value}
            raise exc.HTTPBadRequest(explanation=msg)
        # Let the caller deal with unhandled exceptions.

        # If the caller wanted a reservation_id, return it
        if ret_resv_id:
            return {'reservation_id': resv_id}

        # Instances is a list
        instance = instances[0]
        if not instance.get('_is_precooked', False):
            instance['instance_type'] = inst_type
            instance['image_ref'] = image_href

        server = self._build_view(req, instance, is_create=True)
        if '_is_precooked' in server['server']:
            del server['server']['_is_precooked']
        else:
            server['server']['adminPass'] = password
        return server

    def _delete(self, context, id):
        instance = self._get_server(context, id)
        if FLAGS.reclaim_instance_interval:
            self.compute_api.soft_delete(context, instance)
        else:
            self.compute_api.delete(context, instance)

    @scheduler_api.redirect_handler
    def update(self, req, id, body):
        """Update server then pass on to version-specific controller"""
        if len(req.body) == 0:
            raise exc.HTTPUnprocessableEntity()

        if not body:
            raise exc.HTTPUnprocessableEntity()

        ctxt = req.environ['nova.context']
        update_dict = {}

        if 'name' in body['server']:
            name = body['server']['name']
            self._validate_server_name(name)
            update_dict['display_name'] = name.strip()

        if 'accessIPv4' in body['server']:
            access_ipv4 = body['server']['accessIPv4']
            update_dict['access_ip_v4'] = access_ipv4.strip()

        if 'accessIPv6' in body['server']:
            access_ipv6 = body['server']['accessIPv6']
            update_dict['access_ip_v6'] = access_ipv6.strip()

        if 'auto_disk_config' in body['server']:
            auto_disk_config = utils.bool_from_str(
                    body['server']['auto_disk_config'])
            update_dict['auto_disk_config'] = auto_disk_config

        try:
            self.compute_api.update(ctxt, id, **update_dict)
        except exception.NotFound:
            raise exc.HTTPNotFound()

        instance = self.compute_api.routing_get(ctxt, id)
        return self._build_view(req, instance, is_detail=True)

    @exception.novaclient_converter
    @scheduler_api.redirect_handler
    def action(self, req, id, body):
        """Multi-purpose method used to take actions on a server"""

        self.actions = {
            'changePassword': self._action_change_password,
            'reboot': self._action_reboot,
            'resize': self._action_resize,
            'confirmResize': self._action_confirm_resize,
            'revertResize': self._action_revert_resize,
            'rebuild': self._action_rebuild,
            'createImage': self._action_create_image,
        }

        if FLAGS.allow_admin_api:
            admin_actions = {
                'createBackup': self._action_create_backup,
            }
            self.actions.update(admin_actions)

        for key in body:
            if key in self.actions:
                return self.actions[key](body, req, id)
            else:
                msg = _("There is no such server action: %s") % (key,)
                raise exc.HTTPBadRequest(explanation=msg)

        msg = _("Invalid request body")
        raise exc.HTTPBadRequest(explanation=msg)

    def _action_create_backup(self, input_dict, req, instance_id):
        """Backup a server instance.

        Images now have an `image_type` associated with them, which can be
        'snapshot' or the backup type, like 'daily' or 'weekly'.

        If the image_type is backup-like, then the rotation factor can be
        included and that will cause the oldest backups that exceed the
        rotation factor to be deleted.

        """
        context = req.environ["nova.context"]
        entity = input_dict["createBackup"]

        try:
            image_name = entity["name"]
            backup_type = entity["backup_type"]
            rotation = entity["rotation"]

        except KeyError as missing_key:
            msg = _("createBackup entity requires %s attribute") % missing_key
            raise exc.HTTPBadRequest(explanation=msg)

        except TypeError:
            msg = _("Malformed createBackup entity")
            raise exc.HTTPBadRequest(explanation=msg)

        try:
            rotation = int(rotation)
        except ValueError:
            msg = _("createBackup attribute 'rotation' must be an integer")
            raise exc.HTTPBadRequest(explanation=msg)

        # preserve link to server in image properties
        server_ref = os.path.join(req.application_url, 'servers', instance_id)
        props = {'instance_ref': server_ref}

        metadata = entity.get('metadata', {})
        common.check_img_metadata_quota_limit(context, metadata)
        try:
            props.update(metadata)
        except ValueError:
            msg = _("Invalid metadata")
            raise exc.HTTPBadRequest(explanation=msg)

        instance = self._get_server(context, instance_id)

        image = self.compute_api.backup(context,
                                        instance,
                                        image_name,
                                        backup_type,
                                        rotation,
                                        extra_properties=props)

        # build location of newly-created image entity
        image_id = str(image['id'])
        image_ref = os.path.join(req.application_url, 'images', image_id)

        resp = webob.Response(status_int=202)
        resp.headers['Location'] = image_ref
        return resp

    def _action_confirm_resize(self, input_dict, req, id):
        context = req.environ['nova.context']
        instance = self._get_server(context, id)
        try:
            self.compute_api.confirm_resize(context, instance)
        except exception.MigrationNotFound:
            msg = _("Instance has not been resized.")
            raise exc.HTTPBadRequest(explanation=msg)
        except Exception, e:
            LOG.exception(_("Error in confirm-resize %s"), e)
            raise exc.HTTPBadRequest()
        return exc.HTTPNoContent()

    def _action_revert_resize(self, input_dict, req, id):
        context = req.environ['nova.context']
        instance = self._get_server(context, id)
        try:
            self.compute_api.revert_resize(context, instance)
        except exception.MigrationNotFound:
            msg = _("Instance has not been resized.")
            raise exc.HTTPBadRequest(explanation=msg)
        except Exception, e:
            LOG.exception(_("Error in revert-resize %s"), e)
            raise exc.HTTPBadRequest()
        return webob.Response(status_int=202)

    def _action_reboot(self, input_dict, req, id):
        if 'reboot' in input_dict and 'type' in input_dict['reboot']:
            valid_reboot_types = ['HARD', 'SOFT']
            reboot_type = input_dict['reboot']['type'].upper()
            if not valid_reboot_types.count(reboot_type):
                msg = _("Argument 'type' for reboot is not HARD or SOFT")
                LOG.exception(msg)
                raise exc.HTTPBadRequest(explanation=msg)
        else:
            msg = _("Missing argument 'type' for reboot")
            LOG.exception(msg)
            raise exc.HTTPBadRequest(explanation=msg)

        context = req.environ['nova.context']
        instance = self._get_server(context, id)

        try:
            self.compute_api.reboot(context, instance, reboot_type)
        except Exception, e:
            LOG.exception(_("Error in reboot %s"), e)
            raise exc.HTTPUnprocessableEntity()
        return webob.Response(status_int=202)

    @exception.novaclient_converter
    @scheduler_api.redirect_handler
    def diagnostics(self, req, id):
        """Permit Admins to retrieve server diagnostics."""
        ctxt = req.environ["nova.context"]
        instance = self._get_server(ctxt, id)
        return self.compute_api.get_diagnostics(ctxt, instance)

    def actions(self, req, id):
        """Permit Admins to retrieve server actions."""
        ctxt = req.environ["nova.context"]
        instance = self._get_server(ctxt, id)
        items = self.compute_api.get_actions(ctxt, instance)
        actions = []
        # TODO(jk0): Do not do pre-serialization here once the default
        # serializer is updated
        for item in items:
            actions.append(dict(
                created_at=str(item.created_at),
                action=item.action,
                error=item.error))
        return dict(actions=actions)

    def _resize(self, req, instance_id, flavor_id):
        """Begin the resize process with given instance/flavor."""
        context = req.environ["nova.context"]
        instance = self._get_server(context, instance_id)

        try:
            self.compute_api.resize(context, instance, flavor_id)
        except exception.FlavorNotFound:
            msg = _("Unable to locate requested flavor.")
            raise exc.HTTPBadRequest(explanation=msg)
        except exception.CannotResizeToSameSize:
            msg = _("Resize requires a change in size.")
            raise exc.HTTPBadRequest(explanation=msg)
        except exception.CannotResizeToSmallerSize:
            msg = _("Resizing to a smaller size is not supported.")
            raise exc.HTTPBadRequest(explanation=msg)

        return webob.Response(status_int=202)

    @exception.novaclient_converter
    @scheduler_api.redirect_handler
    def delete(self, req, id):
        """ Destroys a server """
        try:
            self._delete(req.environ['nova.context'], id)
        except exception.NotFound:
            raise exc.HTTPNotFound()

    def _get_key_name(self, req, body):
        if 'server' in body:
            try:
                return body['server'].get('key_name')
            except AttributeError:
                msg = _("Malformed server entity")
                raise exc.HTTPBadRequest(explanation=msg)

    def _image_ref_from_req_data(self, data):
        try:
            return data['server']['imageRef']
        except (TypeError, KeyError):
            msg = _("Missing imageRef attribute")
            raise exc.HTTPBadRequest(explanation=msg)

    def _flavor_id_from_req_data(self, data):
        try:
            flavor_ref = data['server']['flavorRef']
        except (TypeError, KeyError):
            msg = _("Missing flavorRef attribute")
            raise exc.HTTPBadRequest(explanation=msg)

        return common.get_id_from_href(flavor_ref)

    def _build_view(self, req, instance, is_detail=False, is_create=False):
        context = req.environ['nova.context']
        project_id = getattr(context, 'project_id', '')
        base_url = req.application_url
        flavor_builder = views_flavors.ViewBuilder(base_url, project_id)
        image_builder = views_images.ViewBuilder(base_url, project_id)
        addresses_builder = views_addresses.ViewBuilder()
        builder = views_servers.ViewBuilder(context, addresses_builder,
                flavor_builder, image_builder, base_url, project_id)
        return builder.build(instance,
                             is_detail=is_detail,
                             is_create=is_create)

    def _build_list(self, req, instances, is_detail=False):
        params = req.GET.copy()
        pagination_params = common.get_pagination_params(req)
        # Update params with int() values from pagination params
        for key, val in pagination_params.iteritems():
            params[key] = val

        context = req.environ['nova.context']
        project_id = getattr(context, 'project_id', '')
        base_url = req.application_url
        flavor_builder = views_flavors.ViewBuilder(base_url, project_id)
        image_builder = views_images.ViewBuilder(base_url, project_id)
        addresses_builder = views_addresses.ViewBuilder()
        builder = views_servers.ViewBuilder(context, addresses_builder,
                flavor_builder, image_builder, base_url, project_id)
        return builder.build_list(instances, is_detail=is_detail, **params)

    def _action_change_password(self, input_dict, req, id):
        context = req.environ['nova.context']
        if (not 'changePassword' in input_dict
            or not 'adminPass' in input_dict['changePassword']):
            msg = _("No adminPass was specified")
            raise exc.HTTPBadRequest(explanation=msg)
        password = input_dict['changePassword']['adminPass']
        if not isinstance(password, basestring) or password == '':
            msg = _("Invalid adminPass")
            raise exc.HTTPBadRequest(explanation=msg)
        server = self._get_server(context, id)
        self.compute_api.set_admin_password(context, server, password)
        return webob.Response(status_int=202)

    def _limit_items(self, items, req):
        return common.limited_by_marker(items, req)

    def _validate_metadata(self, metadata):
        """Ensure that we can work with the metadata given."""
        try:
            metadata.iteritems()
        except AttributeError as ex:
            msg = _("Unable to parse metadata key/value pairs.")
            LOG.debug(msg)
            raise exc.HTTPBadRequest(explanation=msg)

    def _action_resize(self, input_dict, req, id):
        """ Resizes a given instance to the flavor size requested """
        try:
            flavor_ref = input_dict["resize"]["flavorRef"]
            if not flavor_ref:
                msg = _("Resize request has invalid 'flavorRef' attribute.")
                raise exc.HTTPBadRequest(explanation=msg)
        except (KeyError, TypeError):
            msg = _("Resize requests require 'flavorRef' attribute.")
            raise exc.HTTPBadRequest(explanation=msg)

        return self._resize(req, id, flavor_ref)

    def _action_rebuild(self, info, request, instance_id):
        context = request.environ['nova.context']
        instance = self._get_server(context, instance_id)

        try:
            image_href = info["rebuild"]["imageRef"]
        except (KeyError, TypeError):
            msg = _("Could not parse imageRef from request.")
            LOG.debug(msg)
            raise exc.HTTPBadRequest(explanation=msg)

        personality = info["rebuild"].get("personality", [])
        injected_files = []
        if personality:
            injected_files = self._get_injected_files(personality)

        metadata = info["rebuild"].get("metadata")
        name = info["rebuild"].get("name")

        if metadata:
            self._validate_metadata(metadata)

        if 'rebuild' in info and 'adminPass' in info['rebuild']:
            password = info['rebuild']['adminPass']
        else:
            password = utils.generate_password(FLAGS.password_length)

        try:
            self.compute_api.rebuild(context, instance, image_href,
                                     password, name=name, metadata=metadata,
                                     files_to_inject=injected_files)
        except exception.RebuildRequiresActiveInstance:
            msg = _("Instance %s must be active to rebuild.") % instance_id
            raise exc.HTTPConflict(explanation=msg)
        except exception.InstanceNotFound:
            msg = _("Instance %s could not be found") % instance_id
            raise exc.HTTPNotFound(explanation=msg)

        instance = self._get_server(context, instance_id)
        view = self._build_view(request, instance, is_detail=True)
        view['server']['adminPass'] = password

        return view

    @common.check_snapshots_enabled
    def _action_create_image(self, input_dict, req, instance_id):
        """Snapshot a server instance."""
        context = req.environ['nova.context']
        entity = input_dict.get("createImage", {})

        try:
            image_name = entity["name"]

        except KeyError:
            msg = _("createImage entity requires name attribute")
            raise exc.HTTPBadRequest(explanation=msg)

        except TypeError:
            msg = _("Malformed createImage entity")
            raise exc.HTTPBadRequest(explanation=msg)

        # preserve link to server in image properties
        server_ref = os.path.join(req.application_url, 'servers', instance_id)
        props = {'instance_ref': server_ref}

        metadata = entity.get('metadata', {})
        common.check_img_metadata_quota_limit(context, metadata)
        try:
            props.update(metadata)
        except ValueError:
            msg = _("Invalid metadata")
            raise exc.HTTPBadRequest(explanation=msg)

        instance = self._get_server(context, instance_id)

        try:
            image = self.compute_api.snapshot(context,
                                              instance,
                                              image_name,
                                              extra_properties=props)
        except exception.InstanceBusy:
            msg = _("Server is currently creating an image. Please wait.")
            raise webob.exc.HTTPConflict(explanation=msg)

        # build location of newly-created image entity
        image_id = str(image['id'])
        image_ref = os.path.join(req.application_url,
                                 context.project_id,
                                 'images',
                                 image_id)

        resp = webob.Response(status_int=202)
        resp.headers['Location'] = image_ref
        return resp

    def get_default_xmlns(self, req):
        return common.XML_NS_V11

    def _get_server_admin_password(self, server):
        """ Determine the admin password for a server on creation """
        return self._get_server_admin_password_new_style(server)

    def _get_server_search_options(self):
        """Return server search options allowed by non-admin"""
        return ('reservation_id', 'name', 'local_zone_only',
                'status', 'image', 'flavor', 'changes-since')


class HeadersSerializer(wsgi.ResponseHeadersSerializer):

    def create(self, response, data):
        response.status_int = 202

    def delete(self, response, data):
        response.status_int = 204

    def action(self, response, data):
        response.status_int = 202


class SecurityGroupsTemplateElement(xmlutil.TemplateElement):
    def will_render(self, datum):
        return 'security_groups' in datum


def make_server(elem, detailed=False):
    elem.set('name')
    elem.set('id')

    if detailed:
        elem.set('userId', 'user_id')
        elem.set('tenantId', 'tenant_id')
        elem.set('updated')
        elem.set('created')
        elem.set('hostId')
        elem.set('accessIPv4')
        elem.set('accessIPv6')
        elem.set('status')
        elem.set('progress')

        # Attach image node
        image = xmlutil.SubTemplateElement(elem, 'image', selector='image')
        image.set('id')
        xmlutil.make_links(image, 'links')

        # Attach flavor node
        flavor = xmlutil.SubTemplateElement(elem, 'flavor', selector='flavor')
        flavor.set('id')
        xmlutil.make_links(flavor, 'links')

        # Attach metadata node
        elem.append(common.MetadataTemplate())

        # Attach addresses node
        elem.append(ips.AddressesTemplate())

        # Attach security groups node
        secgrps = SecurityGroupsTemplateElement('security_groups')
        elem.append(secgrps)
        secgrp = xmlutil.SubTemplateElement(secgrps, 'security_group',
                                            selector='security_groups')
        secgrp.set('name')

    xmlutil.make_links(elem, 'links')


server_nsmap = {None: xmlutil.XMLNS_V11, 'atom': xmlutil.XMLNS_ATOM}


class ServerTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('server', selector='server')
        make_server(root, detailed=True)
        return xmlutil.MasterTemplate(root, 1, nsmap=server_nsmap)


class MinimalServersTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('servers')
        elem = xmlutil.SubTemplateElement(root, 'server', selector='servers')
        make_server(elem)
        xmlutil.make_links(root, 'servers_links')
        return xmlutil.MasterTemplate(root, 1, nsmap=server_nsmap)


class ServersTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('servers')
        elem = xmlutil.SubTemplateElement(root, 'server', selector='servers')
        make_server(elem, detailed=True)
        return xmlutil.MasterTemplate(root, 1, nsmap=server_nsmap)


class ServerAdminPassTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('server')
        root.set('adminPass')
        return xmlutil.SlaveTemplate(root, 1, nsmap=server_nsmap)


class ServerXMLSerializer(xmlutil.XMLTemplateSerializer):
    def index(self):
        return MinimalServersTemplate()

    def detail(self):
        return ServersTemplate()

    def show(self):
        return ServerTemplate()

    def update(self):
        return ServerTemplate()

    def create(self):
        master = ServerTemplate()
        master.attach(ServerAdminPassTemplate())
        return master

    def action(self):
        return self.create()


class ServerXMLDeserializer(wsgi.MetadataXMLDeserializer):
    """
    Deserializer to handle xml-formatted server create requests.

    Handles standard server attributes as well as optional metadata
    and personality attributes
    """

    metadata_deserializer = common.MetadataXMLDeserializer()

    def action(self, string):
        dom = minidom.parseString(string)
        action_node = dom.childNodes[0]
        action_name = action_node.tagName

        action_deserializer = {
            'createImage': self._action_create_image,
            'createBackup': self._action_create_backup,
            'changePassword': self._action_change_password,
            'reboot': self._action_reboot,
            'rebuild': self._action_rebuild,
            'resize': self._action_resize,
            'confirmResize': self._action_confirm_resize,
            'revertResize': self._action_revert_resize,
        }.get(action_name, self.default)

        action_data = action_deserializer(action_node)

        return {'body': {action_name: action_data}}

    def _action_create_image(self, node):
        return self._deserialize_image_action(node, ('name',))

    def _action_create_backup(self, node):
        attributes = ('name', 'backup_type', 'rotation')
        return self._deserialize_image_action(node, attributes)

    def _action_change_password(self, node):
        if not node.hasAttribute("adminPass"):
            raise AttributeError("No adminPass was specified in request")
        return {"adminPass": node.getAttribute("adminPass")}

    def _action_reboot(self, node):
        if not node.hasAttribute("type"):
            raise AttributeError("No reboot type was specified in request")
        return {"type": node.getAttribute("type")}

    def _action_rebuild(self, node):
        rebuild = {}
        if node.hasAttribute("name"):
            rebuild['name'] = node.getAttribute("name")

        metadata_node = self.find_first_child_named(node, "metadata")
        if metadata_node is not None:
            rebuild["metadata"] = self.extract_metadata(metadata_node)

        personality = self._extract_personality(node)
        if personality is not None:
            rebuild["personality"] = personality

        if not node.hasAttribute("imageRef"):
            raise AttributeError("No imageRef was specified in request")
        rebuild["imageRef"] = node.getAttribute("imageRef")

        return rebuild

    def _action_resize(self, node):
        if not node.hasAttribute("flavorRef"):
            raise AttributeError("No flavorRef was specified in request")
        return {"flavorRef": node.getAttribute("flavorRef")}

    def _action_confirm_resize(self, node):
        return None

    def _action_revert_resize(self, node):
        return None

    def _deserialize_image_action(self, node, allowed_attributes):
        data = {}
        for attribute in allowed_attributes:
            value = node.getAttribute(attribute)
            if value:
                data[attribute] = value
        metadata_node = self.find_first_child_named(node, 'metadata')
        if metadata_node is not None:
            metadata = self.metadata_deserializer.extract_metadata(
                                                        metadata_node)
            data['metadata'] = metadata
        return data

    def create(self, string):
        """Deserialize an xml-formatted server create request"""
        dom = minidom.parseString(string)
        server = self._extract_server(dom)
        return {'body': {'server': server}}

    def _extract_server(self, node):
        """Marshal the server attribute of a parsed request"""
        server = {}
        server_node = self.find_first_child_named(node, 'server')

        attributes = ["name", "imageRef", "flavorRef", "adminPass",
                      "accessIPv4", "accessIPv6"]
        for attr in attributes:
            if server_node.getAttribute(attr):
                server[attr] = server_node.getAttribute(attr)

        metadata_node = self.find_first_child_named(server_node, "metadata")
        if metadata_node is not None:
            server["metadata"] = self.extract_metadata(metadata_node)

        personality = self._extract_personality(server_node)
        if personality is not None:
            server["personality"] = personality

        networks = self._extract_networks(server_node)
        if networks is not None:
            server["networks"] = networks

        security_groups = self._extract_security_groups(server_node)
        if security_groups is not None:
            server["security_groups"] = security_groups

        auto_disk_config = server_node.getAttribute('auto_disk_config')
        if auto_disk_config:
            server['auto_disk_config'] = utils.bool_from_str(auto_disk_config)

        return server

    def _extract_personality(self, server_node):
        """Marshal the personality attribute of a parsed request"""
        node = self.find_first_child_named(server_node, "personality")
        if node is not None:
            personality = []
            for file_node in self.find_children_named(node, "file"):
                item = {}
                if file_node.hasAttribute("path"):
                    item["path"] = file_node.getAttribute("path")
                item["contents"] = self.extract_text(file_node)
                personality.append(item)
            return personality
        else:
            return None

    def _extract_networks(self, server_node):
        """Marshal the networks attribute of a parsed request"""
        node = self.find_first_child_named(server_node, "networks")
        if node is not None:
            networks = []
            for network_node in self.find_children_named(node,
                                                         "network"):
                item = {}
                if network_node.hasAttribute("uuid"):
                    item["uuid"] = network_node.getAttribute("uuid")
                if network_node.hasAttribute("fixed_ip"):
                    item["fixed_ip"] = network_node.getAttribute("fixed_ip")
                networks.append(item)
            return networks
        else:
            return None

    def _extract_security_groups(self, server_node):
        """Marshal the security_groups attribute of a parsed request"""
        node = self.find_first_child_named(server_node, "security_groups")
        if node is not None:
            security_groups = []
            for sg_node in self.find_children_named(node, "security_group"):
                item = {}
                name_node = self.find_first_child_named(sg_node, "name")
                if name_node:
                    item["name"] = self.extract_text(name_node)
                    security_groups.append(item)
            return security_groups
        else:
            return None


def create_resource():
    headers_serializer = HeadersSerializer()
    body_serializers = {'application/xml': ServerXMLSerializer()}
    serializer = wsgi.ResponseSerializer(body_serializers, headers_serializer)
    body_deserializers = {'application/xml': ServerXMLDeserializer()}
    deserializer = wsgi.RequestDeserializer(body_deserializers)
    return wsgi.Resource(Controller(), deserializer, serializer)


def remove_invalid_options(context, search_options, allowed_search_options):
    """Remove search options that are not valid for non-admin API/context"""
    if FLAGS.allow_admin_api and context.is_admin:
        # Allow all options
        return
    # Otherwise, strip out all unknown options
    unknown_options = [opt for opt in search_options
            if opt not in allowed_search_options]
    unk_opt_str = ", ".join(unknown_options)
    log_msg = _("Removing options '%(unk_opt_str)s' from query") % locals()
    LOG.debug(log_msg)
    for opt in unknown_options:
        search_options.pop(opt, None)
