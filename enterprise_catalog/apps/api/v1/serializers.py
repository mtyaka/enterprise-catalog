import logging
from re import search

from django.db import IntegrityError, models
from rest_framework import serializers, status

from enterprise_catalog.apps.api.v1.utils import (
    get_enterprise_utm_context,
    get_most_recent_modified_time,
    is_any_course_run_active,
    update_query_parameters,
)
from enterprise_catalog.apps.catalog.constants import (
    COURSE,
    COURSE_RUN,
    PROGRAM,
)
from enterprise_catalog.apps.catalog.models import (
    CatalogQuery,
    ContentMetadata,
    EnterpriseCatalog,
)
from enterprise_catalog.apps.catalog.utils import get_content_filter_hash


logger = logging.getLogger(__name__)


def find_and_modify_catalog_query(
        content_filter, catalog_query_uuid=None, query_title=None, include_exec_ed_2u_courses=False
):
    """
    This method aims to make sure UUID, query title and content_filter in the catalog service
    match what Django Admin/passed in parameters have. We take the parameters as source of truth,
    but do not want to duplicate UUID, title or content filter.

    Arguments:
        content_filter(dict): filter used to pick which courses are retrieved
        catalog_query_uuid(UUID/str): query uuid generated from LMS Django Admin.
            - If not provided, we should be receiving a "direct" content filter.
        query_title(str): query title created in LMS Django Admin.
            - Can be null.
        include_exec_ed_2u_courses(bool): Whether exec. ed. courses are allowed to be included in a catalog query.
            - Defaults to False.
    Returns:
        a CatalogQuery object.
    """
    hashed_content_filter = get_content_filter_hash(content_filter)
    if catalog_query_uuid:
        catalog_query_from_uuid = CatalogQuery.get_by_uuid(uuid=catalog_query_uuid)
        if catalog_query_from_uuid:
            catalog_query_from_uuid.content_filter = content_filter
            catalog_query_from_uuid.title = query_title
            catalog_query_from_uuid.content_filter_hash = hashed_content_filter
            catalog_query_from_uuid.include_exec_ed_2u_courses = include_exec_ed_2u_courses
            try:
                catalog_query_from_uuid.save()
            except IntegrityError as exc:
                column = search("(?<=for key ')(.*)(?=')", str(exc))
                logger.exception(f'Error occurred while saving catalog query: {exc}')  # pylint:disable=logging-fstring-interpolation
                raise serializers.ValidationError(
                    {'catalog_query': f'{column} is not unique'},
                    code=status.HTTP_422_UNPROCESSABLE_ENTITY
                ) from exc
            return catalog_query_from_uuid
        else:
            content_filter_from_hash, _ = CatalogQuery.objects.update_or_create(
                content_filter_hash=hashed_content_filter,
                include_exec_ed_2u_courses=include_exec_ed_2u_courses,
                defaults={'content_filter': content_filter, 'uuid': catalog_query_uuid, 'title': query_title}
            )
            return content_filter_from_hash
    else:
        content_filter_from_hash, _ = CatalogQuery.objects.get_or_create(
            content_filter_hash=hashed_content_filter,
            include_exec_ed_2u_courses=include_exec_ed_2u_courses,
            defaults={'content_filter': content_filter, 'title': query_title}
        )

        return content_filter_from_hash


class EnterpriseCatalogSerializer(serializers.ModelSerializer):
    """
    Serializer for the `EnterpriseCatalog` model
    """
    enterprise_customer = serializers.UUIDField(source='enterprise_uuid')
    enterprise_customer_name = serializers.CharField(source='enterprise_name', write_only=True)
    enabled_course_modes = serializers.JSONField(write_only=True)
    publish_audit_enrollment_urls = serializers.BooleanField(write_only=True)
    content_filter = serializers.JSONField(write_only=True)
    catalog_query_uuid = serializers.UUIDField(required=False, allow_null=True)
    catalog_modified = serializers.DateTimeField(source='modified', required=False)
    content_last_modified = serializers.SerializerMethodField()
    query_title = serializers.CharField(allow_null=True, required=False)
    # TODO: we should disallow null after rolling out edx-enterprise changes.
    include_exec_ed_2u_courses = serializers.BooleanField(allow_null=True, required=False)

    class Meta:
        model = EnterpriseCatalog
        fields = [
            'uuid',
            'title',
            'enterprise_customer',
            'enterprise_customer_name',
            'enabled_course_modes',
            'publish_audit_enrollment_urls',
            'content_filter',
            'catalog_query_uuid',
            'content_last_modified',
            'catalog_modified',
            'query_title',
            'include_exec_ed_2u_courses',
        ]

    def get_content_last_modified(self, obj):
        return obj.content_metadata.aggregate(models.Max('modified')).get('modified__max')

    def create(self, validated_data):
        content_filter = validated_data.pop('content_filter')
        catalog_query_uuid = validated_data.pop('catalog_query_uuid', None)
        query_title = validated_data.pop('query_title', None)
        include_exec_ed_2u_courses = validated_data.pop('include_exec_ed_2u_courses', False)

        catalog_query = find_and_modify_catalog_query(
            content_filter,
            catalog_query_uuid,
            query_title,
            include_exec_ed_2u_courses,
        )

        try:
            catalog = EnterpriseCatalog.objects.create(
                **validated_data,
                catalog_query=catalog_query
            )
        except IntegrityError as exc:
            message = (
                'Encountered the following error in the create serializer: %s | '
                'content_filter: %s | '
                'catalog_query id: %s | '
                'validated_data: %s'
            )
            logger.error(message, exc, content_filter, catalog_query.id, validated_data)
            raise

        return catalog

    def update(self, instance, validated_data):
        default_content_filter = None
        default_query_title = None
        default_query_uuid = None
        default_include_exec_ed_2u_courses = False
        if instance.catalog_query:
            default_content_filter = instance.catalog_query.content_filter
            default_query_title = instance.catalog_query.title if hasattr(instance.catalog_query, 'title') else None
            default_query_uuid = str(instance.catalog_query.uuid)
            default_include_exec_ed_2u_courses = instance.catalog_query.include_exec_ed_2u_courses

        content_filter = validated_data.get('content_filter', default_content_filter)
        query_title = validated_data.get('query_title', default_query_title)
        catalog_query_uuid = validated_data.pop('catalog_query_uuid', default_query_uuid)
        include_exec_ed_2u_courses = validated_data.pop(
            'include_exec_ed_2u_courses',
            default_include_exec_ed_2u_courses,
        )
        instance.catalog_query = find_and_modify_catalog_query(
            content_filter,
            catalog_query_uuid,
            query_title,
            include_exec_ed_2u_courses,
        )
        return super().update(instance, validated_data)


class EnterpriseCatalogCreateSerializer(EnterpriseCatalogSerializer):
    """
    Serializer for POST requests on the `EnterpriseCatalog` model

    UUID is writable to allow importing existing Enterprise Catalogs and keeping the same UUID
    """
    uuid = serializers.UUIDField(read_only=False, required=False)


class ImmutableStateSerializer(serializers.Serializer):
    """
    Base serializer for any serializer that inhibits state changing requests.
    """

    def create(self, validated_data):
        """
        Do not perform any operations for state changing requests.
        """

    def update(self, instance, validated_data):
        """
        Do not perform any operations for state changing requests.
        """


class ContentMetadataSerializer(ImmutableStateSerializer):
    """
    Serializer for rendering Content Metadata objects
    """

    def to_representation(self, instance):
        """
        Return the updated content metadata dictionary.

        Arguments:
            instance (dict): ContentMetadata instance.

        Returns:
            dict: The modified json_metadata field.
        """
        enterprise_catalog = self.context['enterprise_catalog']
        content_type = instance.content_type
        json_metadata = instance.json_metadata.copy()
        marketing_url = json_metadata.get('marketing_url')
        content_key = json_metadata.get('key')

        # The enrollment URL field of content metadata is generated on request and is determined by the status of the
        # enterprise customer as well as the catalog. So, in order to detect when content metadata has last been
        # modified, we have to also check the customer and the catalog's modified times.
        modified_time = get_most_recent_modified_time(
            instance.modified,
            enterprise_catalog.modified,
            enterprise_catalog.enterprise_customer.last_modified_date
        )

        json_metadata['content_last_modified'] = modified_time

        if marketing_url:
            marketing_url = update_query_parameters(
                marketing_url,
                get_enterprise_utm_context(enterprise_catalog.enterprise_name)
            )
            json_metadata['marketing_url'] = marketing_url

        if content_type in (COURSE, COURSE_RUN):
            json_metadata['enrollment_url'] = enterprise_catalog.get_content_enrollment_url(instance)
            json_metadata['xapi_activity_id'] = enterprise_catalog.get_xapi_activity_id(
                content_resource=content_type,
                content_key=content_key,
            )
            if content_type == COURSE:
                serialized_course_runs = json_metadata.get('course_runs', [])
                json_metadata['active'] = is_any_course_run_active(serialized_course_runs)
                # We don't include enrollment_url values for the nested course runs in a course
                # for exec-ed-2u content, because enrollment fulfillment for such content
                # is controlled via Entitlements, which are tied directly to Courses
                # (as opposed to Seats, which are tied to Course Runs).
                if not instance.is_exec_ed_2u_course:
                    self._add_course_run_enrollment_urls(instance, serialized_course_runs)
        elif content_type == PROGRAM:
            # We want this to be null, because we have no notion
            # of directly enrolling in a program.
            json_metadata['enrollment_url'] = None

        return json_metadata

    def _add_course_run_enrollment_urls(self, course_instance, serialized_course_runs):
        """
        For the given `course_instance`, computes the enrollment url for each
        child course run and adds it to the serialized representation of the
        course run record in `serialized_course_runs`.
        """
        urls_by_course_run_key = {}
        for course_run in ContentMetadata.get_child_records(course_instance):
            urls_by_course_run_key[course_run.content_key] = \
                self.context['enterprise_catalog'].get_content_enrollment_url(course_run)

        for serialized_run in serialized_course_runs:
            serialized_run['enrollment_url'] = urls_by_course_run_key.get(serialized_run['key'])
