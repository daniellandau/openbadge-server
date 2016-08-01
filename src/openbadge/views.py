from functools import wraps

import analysis
import simplejson
from dateutil.parser import parse as parse_date
from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseNotFound, JsonResponse
from django.shortcuts import render
from rest_framework.decorators import api_view
from .decorators import app_view, is_god, is_own_project
from .models import Meeting, Project, Hub, Chunk  # ActionDataChunk, SamplesDataChunk

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class HttpResponseUnauthorized(HttpResponse):
    status_code = 401


def json_response(**kwargs):
    return HttpResponse(simplejson.dumps(kwargs))


def context(**extra):
    return dict(**extra)


@app_view
def test(request):
    return json_response(success=True)


def test_error(request):
    raise simplejson.JSONDecodeError
    return HttpResponse()


def render_to(template):
    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            out = func(request, *args, **kwargs)
            if isinstance(out, dict):
                out = render(request, template, out)
            return out

        return wrapper

    return decorator


## No-Group views ######################################################################################################

###########################
# Project Level Endpoints #
###########################

@app_view
@api_view(['PUT', 'GET'])
def projects(request):
    if (request.method == 'PUT'):
        return put_project(request)
    elif (request.method == 'GET'):
        return get_project(request)
    return HttpResponseNotFound()


@is_god
@api_view(['PUT'])
def put_project(request):
    return json_response(status="Not Implemented")


@api_view(['GET'])
def get_project(request):
    hub_uuid = request.META.get("HTTP_X_HUB_UUID")
    """ng-device's id for the hub submitting this request"""

    if not hub_uuid:
        return HttpResponseBadRequest()

    try:
        hub = Hub.objects.prefetch_related("project").get(uuid=hub_uuid)
    except Hub.DoesNotExist:
        return HttpResponseNotFound()

    project = hub.project  # type: Project

    return JsonResponse(project.to_object())


###########################
# Meeting Level Endpoints #
###########################

@is_own_project
@app_view
@api_view(['PUT', 'GET', 'POST'])
def meetings(request, project_key):
    if request.method == 'PUT':
        return put_meeting(request, project_key)
    elif request.method == 'GET':
        return get_meeting(request, project_key)
    elif request.method == 'POST':
        return post_meeting(request, project_key)
    return HttpResponseNotFound()


@api_view(['PUT'])
def put_meeting(request, project_key):
    log_file = request.FILES.get("file")

    hub_uuid = request.META.get("HTTP_X_HUB_UUID")

    meeting_info = simplejson.loads(log_file.readline())
    log_file.seek(0)

    meeting_uuid = meeting_info["uuid"]

    try:
        meeting = Meeting.objects.get(uuid=meeting_uuid)
        if meeting.hub.uuid != hub_uuid:
            return HttpResponseUnauthorized()

    except Meeting.DoesNotExist:
        meeting = Meeting()
        meeting.uuid = meeting_uuid
        meeting.project = Project.objects.get(key=project_key)

    try:
        log_file.seek(-2, 2)  # Jump to the second last byte.
        while log_file.read(1) != b"\n":  # Until EOL is found...
            log_file.seek(-2, 1)  # ...jump back the read byte plus one more.

        last = log_file.readline()  # Read last line.

        last_log = simplejson.loads(last)
        meeting.last_update_serial = last_log['last_log_serial']
        meeting.last_update_time = last_log['last_log_time']
    except IOError:
        pass

    log_file.seek(0)

    meeting.log_file = log_file

    meeting.hub = Hub.objects.get(uuid=hub_uuid)

    meeting.start_time = parse_date(meeting_info["start_time"])

    meeting.location = meeting_info["location"]
    meeting.type = meeting_info["type"]
    meeting.description = meeting_info["description"]

    meeting.is_complete = request.data["is_complete"] == 'true' if 'is_complete' in request.data else False

    if meeting.is_complete:
        meeting.ending_method = request.data["ending_method"] if 'ending_method' in request.data else None
        meeting.end_time_string = request.data["end_time"] if 'end_time' in request.data else None
        meeting.end_time = parse_date(meeting.end_time_string) if meeting.end_time_string else \
            meeting.get_last_sample_time()[0]

    meeting.save()

    if meeting.is_complete and settings.SEND_POST_MEETING_SURVEY:
        analysis.post_meeting_analysis(meeting)

    return JsonResponse({'detail': 'meeting created'})


@api_view(['GET'])
def get_meeting(request, project_key):
    try:
        project = Project.objects.prefetch_related("meetings").get(key=project_key)
        get_file = str(request.META.get("HTTP_X_GET_FILE")).lower() == "true"

        return JsonResponse(project.get_meetings(get_file))

    except Project.DoesNotExist:
        return HttpResponseNotFound()


@api_view(['POST'])
def post_meeting(request, project_key):

    api_version = request.data.get('api_version')

    if api_version == "2.0":

        chunks = simplejson.loads(request.data.get('chunks'))

        if len(chunks) == 0:
            print "No data received from", request.META.get("HTTP_X_HUB_UUID")
            return JsonResponse({"status": "no data"})

        try:
            meeting = Meeting.objects.prefetch_related('chunks').get(uuid=request.data.get('uuid'))
        except Meeting.DoesNotExist:
            meeting = Meeting()
            meeting.project = Project.objects.get(key = project_key)
            meeting.hub = Hub.objects.get(uuid = request.META.get("HTTP_X_HUB_UUID"))
            post_meta = chunks[0]
            new_meta = Chunk(event=post_meta['event'],
                             log_index=post_meta['log_index'],
                             log_timestamp=post_meta['log_timestamp'],
                             data=post_meta['data'])
            new_meta.meeting = meeting
            new_meta.save()

            meeting.final_chunk= new_meta

        post_start_serial = chunks[0]['log_serial']

        if post_start_serial > meeting.final_chunk.log_index + 1:
            print "Missed a chunk? Resend Log File.", meeting.uuid
            return JsonResponse({"status": "log mismatch"})

        for post_chunk in chunks:
            try:
                db_chunk = meeting.chunks.get(log_index=post_chunk.log_index)
            except Chunk.DoesNotexist:
                db_chunk = Chunk()
                db_chunk.log_index = post_chunk.log_index
                db_chunk.meeting = meeting

            db_chunk.event = post_chunk.event
            db_chunk.log_timestamp = post_chunk.log_timestamp
            db_chunk.data = post_chunk.data

            db_chunk.save()

        meeting.final_chunk = meeting.chunks.get(log_index=chunks[-1].log_index)
        meeting.save()






    meeting = Meeting.objects.get(uuid=request.data.get('uuid'))
    chunks = (request.data.get('chunks'))
    meeting.is_complete = False  # Make sure we always close a meeting with a PUT.
    update_serial = None
    update_time = None

    print meeting.hub.name + " appending",
    chunks = simplejson.loads(chunks)
    if len(chunks) == 0:
        print " NO CHUNKS",
    else:
        post_start_serial = simplejson.loads(chunks[0])['last_log_serial']
        if post_start_serial != meeting.last_update_serial + 1:
            #meeting.last_update_serial = -1
            meeting.save()
            return JsonResponse({"status": "log mismatch"})
        print "chunks",

        print update_serial,
    log = meeting.log_file.file.name
    with open(log, 'a') as f:
        for chunk in chunks:
            chunk_obj = simplejson.loads(chunk)
            update_time = chunk_obj['last_log_time']
            update_serial = chunk_obj['last_log_serial']
            f.write(chunk)

    print "to", meeting


    if update_time and update_serial:
        meeting.last_update_time = update_time      # simplejson.loads(chunks[-1])['last_log_time']
        meeting.last_update_serial = update_serial  # simplejson.loads(chunks[-1])['last_log_serial']

    meeting.save()

    return JsonResponse({"status": "success"})


#######################
# Hub Level Endpoints #
#######################

@app_view
@api_view(['PUT', 'GET', 'POST'])
def hubs(request, project_key):
    if request.method == 'PUT':
        return put_hubs(request, project_key)
    elif request.method == 'GET':
        return get_hubs(request, project_key)
    elif request.method == 'POST':
        return post_hubs(request, project_key)
    return HttpResponseNotFound()


@api_view(['PUT'])
def put_hubs(request, project_key):
    hub_uuid = request.META.get("HTTP_X_HUB_UUID")
    if hub_uuid:
        hub = Hub()
        hub.uuid = hub_uuid
        hub.project = Project.objects.get(name="OB-DEFAULT")
        hub.name = "New Hub"
        hub.save()
        return HttpResponse()

    return HttpResponseBadRequest()


@is_own_project
@api_view(['GET'])
def get_hubs(request, project_key):
    hub_uuid = request.META.get("HTTP_X_HUB_UUID")
    if not hub_uuid:
        return HttpResponseBadRequest()
    try:
        hub = Hub.objects.get(uuid=hub_uuid)
    except Hub.DoesNotExist:
        return HttpResponseNotFound()

    return JsonResponse(hub.get_object())


@is_own_project
@is_god
@api_view(['POST'])
def post_hubs(request, project_key):
    return JsonResponse({"status": "Not Implemented"})


#########################
# Badge Level Endpoints #
#########################

@is_own_project
@app_view
@api_view(['PUT', 'GET', 'POST'])
def members(request, project_key):
    if request.method == 'PUT':
        return put_members(request, project_key)
    elif request.method == 'GET':
        return get_members(request, project_key)
    elif request.method == 'POST':
        return post_members(request, project_key)
    return HttpResponseNotFound()


@is_god
@api_view(['PUT'])
def put_members(request, project_key):
    return JsonResponse({"status": "Not Implemented"})


@is_god
@api_view(['GET'])
def get_members(request, project_key):
    return JsonResponse({"status": "Not Implemented"})


@api_view(['POST'])
def post_members(request, project_key):

    return JsonResponse({"status": "Not Implemented"})


## Report views ########################################################################################################

# # @user_passes_test(lambda u: u.is_superuser)
# def weekly_group_report(request, group_key, week_num):
#     try:
#         info = WeeklyGroupReport.objects.get(group_key=group_key, week_num=week_num).to_dict()
#         group = StudyGroup.objects.get(key=group_key)
#         name = group.name
#     except WeeklyGroupReport.DoesNotExist:
#         return render(request, 'openbadge/report_template.html',
#                       {'exist': False, 'key': group_key, 'week_num': week_num})
#
#     info['start_date'] = datetime.datetime.strptime(info['start_date'], "%Y-%m-%d").strftime("%A, %B %d")
#     info['end_date'] = datetime.datetime.strptime(info['end_date'], "%Y-%m-%d").strftime("%A, %B %d")
#     info['longest_meeting_date'] = datetime.datetime.strptime(info['longest_meeting_date'], "%A %Y-%m-%d").strftime(
#         "%A, %B %d")
#
#     images = ['location_meeting_count', 'type_meeting_count', 'daily_meeting_time', 'daily_turns_rate',
#               'longest_meeting_turns']
#     paths = {}
#     for image in images:
#         paths[image] = "img/weekly_group_reports/" + group_key + "/week_" + week_num + "_" + image + ".png"
#
#     report_week_num = str(int(week_num) + 2)
#     return render(request, 'openbadge/report_template.html',
#                   {'exist': True, 'paths': paths, 'info': info, 'name': name, 'week_num': report_week_num})
#
#
# @user_passes_test(lambda u: u.is_superuser)
# def internal_report(request):
#     groups = StudyGroup.objects.all().order_by('name')
#
#     durations = []
#     num_meetings = []
#     names = []
#
#     dates = [datetime.date(2016, 6, 13) + datetime.timedelta(days=i) for i in xrange(28)]
#
#     for s_group in groups:
#         meetings = Meeting.objects.filter(group__key=s_group.key, is_complete=True).all()
#
#         num_meet_temp = []
#         time_meet_temp = []
#
#         for current in dates:
#             meets = [meet for meet in meetings
#                      if (meet.start_time.year == current.year and
#                          meet.start_time.month == current.month and
#                          meet.start_time.day == current.day)]
#
#             num_meet_temp.append(len(meets))
#             times = [entry.end_time - entry.start_time for entry in meets]
#             time_meet_temp.append((sum(times, datetime.timedelta()).total_seconds()) / 3600.0)
#
#         num_meetings.append(num_meet_temp)
#         durations.append(time_meet_temp)
#         names.append(s_group.name)
#
#     graph_path = settings.MEDIA_ROOT + '/tmp'
#
#     try:
#         os.mkdir(graph_path)
#     except OSError:
#         if not os.path.isdir(graph_path):
#             raise
#
#     metadata = groupStatGraph(durations, num_meetings, dates, names, graph_path)
#
#     return render(request, 'reports/internal_report.html', {'metadata': metadata})
